"""
Hochschul-Insights — Fabric one-click installer.

Deploys an end-to-end German higher-education insights demo into a Microsoft
Fabric workspace:

  DESTATIS GENESIS REST API
    -> WebinarLakehouse (Delta, schema "Genesis")
    -> Hochschule Semantic Model (Direct Lake)
    -> Webinar Hochschule Report (8 pages)
    + Hochschul-Insights GENESIS Pipeline (orchestration)
    + Hochschul-Stats-Agent (DataAgent for natural-language Q&A)

Usage
-----
    # Recommended one-liner (PowerShell):
    $env:FABRIC_WORKSPACE_ID = "<workspace-guid>"
    $env:GENESIS_TOKEN       = "<destatis-genesis-token>"
    python -m pip install --quiet requests azure-identity
    python -c "import urllib.request as u; exec(u.urlopen('https://raw.githubusercontent.com/KornAlexander/Fabric-Demos/main/Hochschul-Insights/install.py').read())"

    # Or run from a clone:
    python install.py --workspace-id <guid> --genesis-token <token>

Auth
----
Uses azure-identity DefaultAzureCredential (az CLI / VS Code / env / interactive
browser). Make sure you are logged in to the *Fabric tenant* of your workspace.

Get a DESTATIS GENESIS token (free)
-----------------------------------
1. Register: https://www-genesis.destatis.de/
2. Profile -> "Token zuruecksetzen" -> copy your username token.
3. Free tier is sufficient for this demo (no premium features used).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from typing import Any

import requests

REPO_RAW = "https://raw.githubusercontent.com/KornAlexander/Fabric-Demos/main/Hochschul-Insights/templates"
FABRIC = "https://api.fabric.microsoft.com/v1"
FOLDER_NAME = "Hochschul-Insights"

# Local-or-remote template loader -------------------------------------------------

def load_template(name: str) -> dict:
    """Load a template either from the same folder as this script (local clone)
    or from the GitHub raw URL (one-liner / Fabric-notebook mode)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.join(here, "templates", name)
        if os.path.isfile(local):
            with open(local, encoding="utf-8") as f:
                return json.load(f)
    except NameError:
        pass  # __file__ undefined when loaded via exec() inside a Fabric notebook
    url = f"{REPO_RAW}/{name}"
    with urllib.request.urlopen(url) as r:  # noqa: S310 — public raw URL
        return json.loads(r.read().decode("utf-8"))


# Placeholder substitution ------------------------------------------------------

def substitute(definition: dict, mapping: dict[str, str]) -> dict:
    """Decode every text part, replace placeholders, re-encode."""
    out = json.loads(json.dumps(definition))  # deep copy
    for p in out["definition"]["parts"]:
        try:
            text = base64.b64decode(p["payload"]).decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary part, leave alone
        for k, v in mapping.items():
            text = text.replace(f"__{k}__", v)
        p["payload"] = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return out


# Auth --------------------------------------------------------------------------

def _get_token(scope: str) -> str:
    """Acquire a bearer token. Uses notebookutils inside Fabric, azure-identity locally."""
    try:
        import notebookutils  # type: ignore
        # notebookutils audiences: 'pbi' -> analysis.windows.net/powerbi/api (works for Fabric API too)
        aud = "pbi" if "powerbi" in scope or "fabric" in scope else "storage"
        return notebookutils.credentials.getToken(aud)
    except Exception:
        pass
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        sys.exit("ERROR: azure-identity not installed. Run: pip install azure-identity requests")
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return cred.get_token(scope).token


def get_token() -> str:
    """Token for Fabric REST API."""
    return _get_token("https://api.fabric.microsoft.com/.default")


def get_pbi_token() -> str:
    """Token for Power BI REST API (used for semantic model refresh)."""
    return _get_token("https://analysis.windows.net/powerbi/api/.default")


def detect_fabric_workspace_id() -> str | None:
    """When running inside a Fabric notebook, get the current workspace id."""
    try:
        import notebookutils  # type: ignore
        ctx = notebookutils.runtime.context
        return ctx.get("currentWorkspaceId") or ctx.get("workspaceId")
    except Exception:
        return None


# Fabric REST helpers -----------------------------------------------------------

class Fabric:
    def __init__(self, token: str, ws_id: str):
        import requests
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.s.headers["Content-Type"] = "application/json"
        self.ws = ws_id

    def _wait(self, resp) -> dict:
        """Handle 200/201 (sync) and 202 (async) Fabric responses."""
        if resp.status_code in (200, 201):
            return resp.json() if resp.text else {}
        if resp.status_code != 202:
            raise RuntimeError(f"{resp.status_code} {resp.text}")
        loc = resp.headers["Location"]
        result_url = resp.headers.get("Operation-Location", loc) + "/result"
        for _ in range(120):
            time.sleep(2)
            p = self.s.get(loc)
            st = p.json().get("status")
            if st == "Succeeded":
                r = self.s.get(result_url)
                return r.json() if r.text else {}
            if st in ("Failed", "Undefined"):
                raise RuntimeError(f"Operation failed: {p.text}")
        raise TimeoutError("Operation did not finish within 4 min")

    def get_workspace(self) -> dict:
        return self.s.get(f"{FABRIC}/workspaces/{self.ws}").json()

    def create_folder(self, name: str) -> str:
        # Reuse an existing folder of the same name if present
        r = self.s.get(f"{FABRIC}/workspaces/{self.ws}/folders").json()
        for f in r.get("value", []):
            if f.get("displayName") == name and not f.get("parentFolderId"):
                print(f"  Folder '{name}' already exists -> reusing")
                return f["id"]
        body = {"displayName": name}
        r = self.s.post(f"{FABRIC}/workspaces/{self.ws}/folders", json=body)
        return self._wait(r)["id"]

    def create_lakehouse(self, name: str, folder_id: str) -> tuple[str, str]:
        body = {
            "displayName": name,
            "folderId": folder_id,
            "creationPayload": {"enableSchemas": True},
        }
        r = self.s.post(f"{FABRIC}/workspaces/{self.ws}/lakehouses", json=body)
        out = self._wait(r)
        lh_id = out["id"]
        # Wait for SQL endpoint to provision
        for _ in range(30):
            d = self.s.get(f"{FABRIC}/workspaces/{self.ws}/lakehouses/{lh_id}").json()
            ep = (d.get("properties") or {}).get("sqlEndpointProperties") or {}
            if ep.get("provisioningStatus") == "Success":
                return lh_id, ep["id"]
            time.sleep(3)
        return lh_id, ""

    def create_item(self, display_name: str, type_: str, definition: dict, folder_id: str) -> str:
        body = {
            "displayName": display_name,
            "type": type_,
            "definition": definition["definition"],
            "folderId": folder_id,
        }
        r = self.s.post(f"{FABRIC}/workspaces/{self.ws}/items", json=body)
        return self._wait(r)["id"]


# Main --------------------------------------------------------------------------

def install(workspace_id: str | None = None, genesis_token: str | None = None,
            run_pipeline: bool = True, wait_for_pipeline: bool = True,
            pipeline_timeout_min: int = 30) -> dict:
    """Deploy Hochschul-Insights into a Fabric workspace and return new IDs.

    Args:
        workspace_id: Target Fabric workspace GUID. Falls back to env
            ``FABRIC_WORKSPACE_ID`` and then to auto-detection when running
            inside a Fabric notebook.
        genesis_token: DESTATIS GENESIS API token. Falls back to env
            ``GENESIS_TOKEN``.
        run_pipeline: If True (default), trigger the pipeline once after
            install so the lakehouse populates and the Direct Lake model
            lights up. Set False to skip.
        wait_for_pipeline: If True (default) and ``run_pipeline`` is True,
            poll the pipeline to completion, then refresh the semantic model
            (so calculated tables like ``Calendar`` are populated and the
            report renders without manual refresh). Set False for fire-and-
            forget behavior.
        pipeline_timeout_min: Max minutes to wait for the pipeline. Default 30.

    Returns:
        dict with the new item IDs (and ``pipeline_run_id`` /
        ``sm_refresh_id`` if triggered).
    """
    workspace_id = workspace_id or os.environ.get("FABRIC_WORKSPACE_ID") or detect_fabric_workspace_id()
    genesis_token = genesis_token or os.environ.get("GENESIS_TOKEN")

    if not workspace_id:
        raise SystemExit("ERROR: workspace_id required (arg, env FABRIC_WORKSPACE_ID, "
                         "or run inside a Fabric notebook).")

    snapshot_mode = not genesis_token
    if snapshot_mode:
        print("No GENESIS token provided -> SNAPSHOT mode: loading bundled CSVs from GitHub.")
        print("(Register at https://www-genesis.destatis.de/ for a free token to enable LIVE mode.)")

    print("Authenticating ...")
    token = get_token()
    fab = Fabric(token, workspace_id)

    ws = fab.get_workspace()
    ws_name = ws["displayName"]
    print(f"Workspace: {ws_name} ({workspace_id})")

    print(f"\n[1/8] Creating folder '{FOLDER_NAME}' ...")
    folder_id = fab.create_folder(FOLDER_NAME)
    print(f"      folder_id = {folder_id}")

    print(f"\n[2/8] Creating Lakehouse 'WebinarLakehouse' (schemas enabled) ...")
    lh_id, sql_ep_id = fab.create_lakehouse("WebinarLakehouse", folder_id)
    print(f"      lakehouse_id = {lh_id}")
    print(f"      sql_endpoint = {sql_ep_id or '(still provisioning)'}")

    loader_nb_id = None
    snapshot_nb_id = None
    if not snapshot_mode:
        print(f"\n[3/8] Creating loader notebook ...")
        loader_def = substitute(load_template("loader_notebook.json"), {
            "WORKSPACE_ID":  workspace_id,
            "LAKEHOUSE_ID":  lh_id,
            "STALE_LH_ID":   lh_id,
            "STALE_WS_ID":   workspace_id,
            "GENESIS_TOKEN": genesis_token,
        })
        loader_nb_id = fab.create_item("Hochschul-Insights GENESIS Loader", "Notebook", loader_def, folder_id)
        print(f"      loader_nb_id = {loader_nb_id}")
    else:
        print(f"\n[3/8] Creating snapshot loader notebook ...")
        snap_def = substitute(load_template("snapshot_loader.json"), {
            "STALE_LH_ID": lh_id,
            "STALE_WS_ID": workspace_id,
        })
        snapshot_nb_id = fab.create_item("Hochschul-Insights Snapshot Loader", "Notebook", snap_def, folder_id)
        print(f"      snapshot_nb_id = {snapshot_nb_id}")

    print(f"\n[4/8] Creating dimensions notebook ...")
    dims_def = substitute(load_template("dims_notebook.json"), {
        "WORKSPACE_ID": workspace_id,
        "LAKEHOUSE_ID": lh_id,
        "STALE_LH_ID":  lh_id,
        "STALE_WS_ID":  workspace_id,
    })
    dims_nb_id = fab.create_item("Hochschul-Insights GENESIS Dimensions", "Notebook", dims_def, folder_id)
    print(f"      dims_nb_id = {dims_nb_id}")

    print(f"\n[5/8] Creating Direct Lake semantic model 'Hochschule' ...")
    sm_def = substitute(load_template("semantic_model.json"), {
        "WORKSPACE_ID": workspace_id,
        "LAKEHOUSE_ID": lh_id,
    })
    sm_id = fab.create_item("Hochschule", "SemanticModel", sm_def, folder_id)
    print(f"      semanticmodel_id = {sm_id}")

    print(f"\n[6/8] Creating report 'Webinar Hochschule' ...")
    rpt_def = substitute(load_template("report.json"), {
        "SEMANTICMODEL_ID": sm_id,
        "WORKSPACE_NAME":   ws_name,
    })
    rpt_id = fab.create_item("Webinar Hochschule", "Report", rpt_def, folder_id)
    print(f"      report_id = {rpt_id}")

    pl_id = None
    if not snapshot_mode:
        print(f"\n[7/8] Creating pipeline ...")
        pl_def = substitute(load_template("pipeline.json"), {
            "WORKSPACE_ID": workspace_id,
            "LOADER_NB_ID": loader_nb_id,
            "DIMS_NB_ID":   dims_nb_id,
        })
        pl_id = fab.create_item("Hochschul-Insights GENESIS Pipeline", "DataPipeline", pl_def, folder_id)
        print(f"      pipeline_id = {pl_id}")
    else:
        print(f"\n[7/8] Skipping pipeline (snapshot mode).")

    print(f"\n[8/8] Creating DataAgent 'Hochschul-Stats-Agent' ...")
    da_id = None
    try:
        da_def = substitute(load_template("dataagent.json"), {
            "WORKSPACE_ID":     workspace_id,
            "LAKEHOUSE_ID":     lh_id,
            "SEMANTICMODEL_ID": sm_id,
        })
        da_id = fab.create_item("Hochschul-Stats-Agent", "DataAgent", da_def, folder_id)
        print(f"      dataagent_id = {da_id}")
    except Exception as e:
        print(f"      SKIPPED (DataAgent create failed): {e}")
        print(f"      You can recreate it manually from the report's 'Ask a question' tile.")

    pl_run_id = None
    sm_refresh_id = None
    if run_pipeline:
        if snapshot_mode:
            job_label = "snapshot loader notebook"
            job_item_id = snapshot_nb_id
            job_type = "RunNotebook"
        else:
            job_label = "pipeline"
            job_item_id = pl_id
            job_type = "Pipeline"
        print(f"\n[9/9] Triggering {job_label} ...")
        try:
            r = fab.s.post(
                f"{FABRIC}/workspaces/{workspace_id}/items/{job_item_id}/jobs/instances?jobType={job_type}",
                json={},
            )
            r.raise_for_status()
            loc = r.headers.get("Location", "")
            pl_run_id = loc.rsplit("/", 1)[-1] if loc else None
            print(f"      run_id = {pl_run_id}")
            mon_url = (f"https://app.powerbi.com/groups/{workspace_id}/pipelines/{pl_id}" if not snapshot_mode
                       else f"https://app.powerbi.com/groups/{workspace_id}/synapsenotebooks/{snapshot_nb_id}")
            print(f"      Monitor: {mon_url}")

            if wait_for_pipeline and pl_run_id and loc:
                print(f"      Waiting for pipeline to finish (poll every 30s, max {pipeline_timeout_min} min) ...")
                deadline = time.time() + pipeline_timeout_min * 60
                final = None
                while time.time() < deadline:
                    time.sleep(30)
                    pr = fab.s.get(loc).json()
                    print(f"        status={pr.get('status')}")
                    if pr.get("status") in ("Completed", "Failed", "Cancelled", "Deduped"):
                        final = pr
                        break
                if not final:
                    print("      WARN: pipeline did not finish before timeout; skipping refresh.")
                elif final.get("status") != "Completed":
                    print(f"      WARN: pipeline ended with status={final.get('status')}; skipping refresh.")
                    print(f"      failureReason: {final.get('failureReason')}")
                else:
                    # Direct Lake needs the SQL endpoint to discover the new Delta tables.
                    # Force a metadata sync via the SQL endpoint refreshMetadata API, then poll.
                    print(f"\n      Forcing SQL endpoint metadata refresh ...")
                    try:
                        rm = fab.s.post(
                            f"{FABRIC}/workspaces/{workspace_id}/sqlEndpoints/{sql_ep_id}/refreshMetadata?preview=true",
                            json={},
                        )
                        if rm.status_code in (200, 202):
                            rm_loc = rm.headers.get("Location", "")
                            if rm_loc:
                                rmdeadline = time.time() + 180
                                while time.time() < rmdeadline:
                                    time.sleep(10)
                                    rms = fab.s.get(rm_loc).json()
                                    rmst = rms.get("status")
                                    print(f"        sqlendpoint-refresh status={rmst}")
                                    if rmst in ("Succeeded", "Failed", "Completed"):
                                        break
                        else:
                            print(f"        WARN: refreshMetadata returned {rm.status_code} {rm.text[:200]}")
                    except Exception as e:
                        print(f"        WARN: SQL endpoint metadata refresh failed: {e}")
                    print(f"      Waiting 30s safety margin before semantic model refresh ...")
                    time.sleep(30)
                    pbi_tok = get_pbi_token()
                    pbi_s = requests.Session()
                    pbi_s.headers["Authorization"] = f"Bearer {pbi_tok}"
                    final_st = None
                    for refresh_attempt in range(1, 6):
                        print(f"\n      Refreshing semantic model 'Hochschule' (attempt {refresh_attempt}/5) ...")
                        try:
                            rr = pbi_s.post(
                                f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{sm_id}/refreshes",
                                json={"type": "full", "commitMode": "transactional", "applyRefreshPolicy": False},
                            )
                            rr.raise_for_status()
                            refresh_loc = rr.headers.get("Location", "")
                            sm_refresh_id = refresh_loc.rsplit("/", 1)[-1] if refresh_loc else rr.headers.get("RequestId")
                            print(f"      refresh_id = {sm_refresh_id}")
                            rdeadline = time.time() + 5 * 60
                            final_st = None
                            while refresh_loc and time.time() < rdeadline:
                                time.sleep(5)
                                rs = pbi_s.get(refresh_loc).json()
                                final_st = rs.get("status")
                                print(f"        refresh status={final_st}")
                                if final_st in ("Completed", "Failed", "Cancelled"):
                                    break
                            if final_st == "Completed":
                                break
                            if refresh_attempt < 5:
                                print(f"      Refresh status={final_st}; sleeping 45s and retrying ...")
                                time.sleep(45)
                        except Exception as e:
                            print(f"      WARN: semantic model refresh attempt {refresh_attempt} failed: {e}")
                            if refresh_attempt < 5:
                                time.sleep(45)
                    if final_st != "Completed":
                        print(f"      WARN: semantic model refresh did not complete (final status={final_st}).")
        except Exception as e:
            print(f"      SKIPPED (pipeline trigger failed): {e}")
            print(f"      You can run it manually from the workspace.")

    print("\n" + "=" * 60)
    print(f"Done. Open: https://app.powerbi.com/groups/{workspace_id}/list")
    print("\nNext steps:")
    if snapshot_mode:
        print("  1. Snapshot data loaded from bundled CSVs (no DESTATIS token required).")
        print("     To switch to LIVE mode, rerun the installer with a GENESIS_TOKEN.")
    elif run_pipeline and pl_run_id:
        print("  1. Wait ~5-10 min for the pipeline to finish populating the lakehouse.")
        print("     The Direct Lake model will auto-light up once the first load completes.")
    else:
        print("  1. Run the pipeline once to populate the lakehouse (~5-10 min).")
        print("     The Direct Lake model will auto-light up after the first load.")
    print("  2. Open 'Webinar Hochschule' report.")
    if not snapshot_mode:
        print("  3. Schedule the pipeline weekly (DESTATIS updates monthly at most).")

    return {
        "folder_id":        folder_id,
        "lakehouse_id":     lh_id,
        "sql_endpoint_id":  sql_ep_id,
        "loader_nb_id":     loader_nb_id,
        "snapshot_nb_id":   snapshot_nb_id,
        "dims_nb_id":       dims_nb_id,
        "semanticmodel_id": sm_id,
        "report_id":        rpt_id,
        "pipeline_id":      pl_id,
        "dataagent_id":     da_id,
        "mode":             "snapshot" if snapshot_mode else "live",
        "pipeline_run_id":  pl_run_id,
        "sm_refresh_id":    sm_refresh_id,
    }


def main():
    ap = argparse.ArgumentParser(description="Deploy Hochschul-Insights to Microsoft Fabric.")
    ap.add_argument("--workspace-id", default=None,
                    help="Target Fabric workspace GUID (or env FABRIC_WORKSPACE_ID, "
                         "or auto-detected when running inside a Fabric notebook)")
    ap.add_argument("--genesis-token", default=None,
                    help="DESTATIS GENESIS API token (or env GENESIS_TOKEN)")
    ap.add_argument("--no-run-pipeline", action="store_true",
                    help="Skip the initial pipeline run (default: run after install).")
    ap.add_argument("--no-wait", action="store_true",
                    help="Don't wait for pipeline completion or refresh the semantic model (default: wait+refresh).")
    ap.add_argument("--pipeline-timeout-min", type=int, default=30,
                    help="Max minutes to wait for the pipeline (default: 30).")
    args = ap.parse_args()
    install(workspace_id=args.workspace_id, genesis_token=args.genesis_token,
            run_pipeline=not args.no_run_pipeline,
            wait_for_pipeline=not args.no_wait,
            pipeline_timeout_min=args.pipeline_timeout_min)


if __name__ == "__main__":
    main()

