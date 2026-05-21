"""
DWD-Wetter-Insights — Microsoft Fabric one-click installer.

Deploys an end-to-end German weather (DWD) demo into a Microsoft Fabric workspace:

    DWD Climate Data Center (open data, no auth)
      -> DemoLakehouse (Delta, schemas Wetter_dwdown / Wetter_forecast)
      -> Wetter-Insights Semantic Model (Direct Lake on OneLake)
      -> DWD Wetter-Insights Report (4 pages: Home, Ubersicht, Stationen, Vorhersage)
      + DWD-Wetter-Insights Daily Pipeline (Load -> Refresh -> Notify)

Usage
-----
    # Recommended one-liner (PowerShell):
    $env:FABRIC_WORKSPACE_ID = "<workspace-guid>"
    python -m pip install --quiet requests azure-identity
    python -c "import urllib.request as u; exec(u.urlopen('https://raw.githubusercontent.com/KornAlexander/Fabric-Demos/main/DWD-Wetter-Insights/Install-DWD-Wetter-Insights.py').read())"

    # Or run from a clone:
    python Install-DWD-Wetter-Insights.py --workspace-id <guid>

Auth
----
Uses azure-identity DefaultAzureCredential (az CLI / VS Code / env / interactive
browser). Make sure you are logged in to the *Fabric tenant* of your workspace.

No API tokens needed — DWD Climate Data Center is fully open.
The optional Notify notebook sends email via Microsoft Graph using the
notebook user's delegated token (no app registration required).
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

REPO_RAW = "https://raw.githubusercontent.com/KornAlexander/Fabric-Demos/main/DWD-Wetter-Insights/templates"
FABRIC = "https://api.fabric.microsoft.com/v1"
FOLDER_NAME = "DWD-Wetter-Insights"
LAKEHOUSE_NAME = "DemoLakehouse"


def load_template(name: str) -> dict:
    """Load template from local clone or GitHub raw URL."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.join(here, "templates", name)
        if os.path.isfile(local):
            with open(local, encoding="utf-8") as f:
                return json.load(f)
    except NameError:
        pass
    url = f"{REPO_RAW}/{name}"
    with urllib.request.urlopen(url) as r:  # noqa: S310 — public raw URL
        return json.loads(r.read().decode("utf-8"))


def substitute(definition: dict, mapping: dict[str, str]) -> dict:
    """Decode each text part, replace __PLACEHOLDER__ markers, re-encode."""
    out = json.loads(json.dumps(definition))
    for p in out["definition"]["parts"]:
        try:
            text = base64.b64decode(p["payload"]).decode("utf-8")
        except UnicodeDecodeError:
            continue
        for k, v in mapping.items():
            text = text.replace(f"__{k}__", v)
        p["payload"] = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return out


def _get_token(scope: str) -> str:
    try:
        import notebookutils  # type: ignore
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
    return _get_token("https://api.fabric.microsoft.com/.default")


def get_pbi_token() -> str:
    return _get_token("https://analysis.windows.net/powerbi/api/.default")


def detect_fabric_workspace_id() -> str | None:
    try:
        import notebookutils  # type: ignore
        ctx = notebookutils.runtime.context
        return ctx.get("currentWorkspaceId") or ctx.get("workspaceId")
    except Exception:
        return None


class Fabric:
    def __init__(self, token: str, ws_id: str):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.s.headers["Content-Type"] = "application/json"
        self.ws = ws_id

    def _wait(self, resp) -> dict:
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
        r = self.s.get(f"{FABRIC}/workspaces/{self.ws}/folders").json()
        for f in r.get("value", []):
            if f.get("displayName") == name and not f.get("parentFolderId"):
                print(f"  Folder '{name}' already exists -> reusing")
                return f["id"]
        r = self.s.post(f"{FABRIC}/workspaces/{self.ws}/folders", json={"displayName": name})
        return self._wait(r)["id"]

    def create_lakehouse(self, name: str, folder_id: str) -> tuple[str, str]:
        existing = self.s.get(f"{FABRIC}/workspaces/{self.ws}/lakehouses").json().get("value", [])
        for lh in existing:
            if lh.get("displayName") == name:
                print(f"  Lakehouse '{name}' already exists -> reusing")
                d = self.s.get(f"{FABRIC}/workspaces/{self.ws}/lakehouses/{lh['id']}").json()
                ep = (d.get("properties") or {}).get("sqlEndpointProperties") or {}
                return lh["id"], ep.get("id", "")
        body = {
            "displayName": name,
            "folderId": folder_id,
            "creationPayload": {"enableSchemas": True},
        }
        r = self.s.post(f"{FABRIC}/workspaces/{self.ws}/lakehouses", json=body)
        out = self._wait(r)
        lh_id = out["id"]
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


def install(workspace_id: str | None = None, lakehouse_name: str = LAKEHOUSE_NAME,
            run_pipeline: bool = True, wait_for_pipeline: bool = True,
            pipeline_timeout_min: int = 60) -> dict:
    """Deploy DWD-Wetter-Insights into a Fabric workspace and return new IDs."""
    workspace_id = workspace_id or os.environ.get("FABRIC_WORKSPACE_ID") or detect_fabric_workspace_id()
    if not workspace_id:
        raise SystemExit("ERROR: workspace_id required (arg, env FABRIC_WORKSPACE_ID, "
                         "or run inside a Fabric notebook).")

    print("Authenticating ...")
    token = get_token()
    fab = Fabric(token, workspace_id)

    ws = fab.get_workspace()
    ws_name = ws["displayName"]
    print(f"Workspace: {ws_name} ({workspace_id})")

    print(f"\n[1/8] Creating folder '{FOLDER_NAME}' ...")
    folder_id = fab.create_folder(FOLDER_NAME)
    print(f"      folder_id = {folder_id}")

    print(f"\n[2/8] Creating Lakehouse '{lakehouse_name}' (schemas enabled) ...")
    lh_id, sql_ep_id = fab.create_lakehouse(lakehouse_name, folder_id)
    print(f"      lakehouse_id = {lh_id}")
    print(f"      sql_endpoint = {sql_ep_id or '(still provisioning)'}")

    common = {
        "WORKSPACE_ID":   workspace_id,
        "LAKEHOUSE_ID":   lh_id,
        "LAKEHOUSE_NAME": lakehouse_name,
    }

    print(f"\n[3/8] Creating Loader notebook (DWD observations via dwdown) ...")
    loader_nb_id = fab.create_item(
        "DWD-Wetter-Insights Loader", "Notebook",
        substitute(load_template("nb_loader.json"), common), folder_id,
    )
    print(f"      loader_nb_id = {loader_nb_id}")

    print(f"\n[4/8] Creating Forecast Loader notebook (DWD Mosmix) ...")
    forecast_nb_id = fab.create_item(
        "DWD-Wetter-Insights Forecast Loader", "Notebook",
        substitute(load_template("nb_forecast.json"), common), folder_id,
    )
    print(f"      forecast_nb_id = {forecast_nb_id}")

    print(f"\n[5/8] Creating Refresh SM notebook ...")
    # SM ID not yet known — create with placeholder, patch after SM is created.
    refresh_sm_nb_id = fab.create_item(
        "DWD-Wetter-Insights Refresh SM", "Notebook",
        substitute(load_template("nb_refresh_sm.json"), {**common, "SEMANTICMODEL_ID": "00000000-0000-0000-0000-000000000000"}),
        folder_id,
    )
    print(f"      refresh_sm_nb_id = {refresh_sm_nb_id}")

    print(f"\n[6/8] Creating Notify notebook ...")
    notify_nb_id = fab.create_item(
        "DWD-Wetter-Insights Notify", "Notebook",
        substitute(load_template("nb_notify.json"), common), folder_id,
    )
    print(f"      notify_nb_id = {notify_nb_id}")

    print(f"\n[7/8] Creating Direct Lake semantic model 'Wetter-Insights' ...")
    sm_id = fab.create_item(
        "Wetter-Insights", "SemanticModel",
        substitute(load_template("semantic_model.json"), common), folder_id,
    )
    print(f"      semanticmodel_id = {sm_id}")

    # Now patch Refresh SM notebook with the real semantic model id.
    print(f"      patching Refresh SM notebook with semantic model id ...")
    refresh_def = substitute(load_template("nb_refresh_sm.json"), {**common, "SEMANTICMODEL_ID": sm_id})
    fab.s.post(
        f"{FABRIC}/workspaces/{workspace_id}/notebooks/{refresh_sm_nb_id}/updateDefinition",
        json={"definition": refresh_def["definition"]},
    )

    print(f"\n[7b/8] Creating report 'DWD Wetter-Insights' ...")
    rpt_def = substitute(load_template("report.json"), {"SEMANTICMODEL_ID": sm_id})
    rpt_id = fab.create_item("DWD Wetter-Insights", "Report", rpt_def, folder_id)
    print(f"      report_id = {rpt_id}")

    print(f"\n[8/8] Creating Daily pipeline ...")
    pl_def = substitute(load_template("pipeline.json"), {
        "WORKSPACE_ID":        workspace_id,
        "LOADER_NB_ID":        loader_nb_id,
        "REFRESH_SM_NB_ID":    refresh_sm_nb_id,
        "NOTIFY_NB_ID":        notify_nb_id,
    })
    pl_id = fab.create_item("DWD-Wetter-Insights Daily", "DataPipeline", pl_def, folder_id)
    print(f"      pipeline_id = {pl_id}")

    pl_run_id = None
    sm_refresh_id = None
    if run_pipeline:
        print(f"\n[run] Triggering pipeline ...")
        try:
            r = fab.s.post(f"{FABRIC}/workspaces/{workspace_id}/items/{pl_id}/jobs/instances?jobType=Pipeline", json={})
            r.raise_for_status()
            loc = r.headers.get("Location", "")
            pl_run_id = loc.rsplit("/", 1)[-1] if loc else None
            print(f"      pipeline_run_id = {pl_run_id}")
            print(f"      Monitor: https://app.powerbi.com/groups/{workspace_id}/pipelines/{pl_id}")
            if wait_for_pipeline and loc:
                print(f"      Waiting for pipeline (poll every 30s, max {pipeline_timeout_min} min) ...")
                deadline = time.time() + pipeline_timeout_min * 60
                final = None
                while time.time() < deadline:
                    time.sleep(30)
                    pr = fab.s.get(loc).json()
                    st = pr.get("status")
                    print(f"        status={st}")
                    if st in ("Completed", "Failed", "Cancelled", "Deduped"):
                        final = pr
                        break
                if final and final.get("status") == "Completed":
                    print("\n      Refreshing semantic model 'Wetter-Insights' ...")
                    try:
                        pbi_s = requests.Session()
                        pbi_s.headers["Authorization"] = f"Bearer {get_pbi_token()}"
                        rr = pbi_s.post(
                            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{sm_id}/refreshes",
                            json={"type": "full", "commitMode": "transactional", "applyRefreshPolicy": False},
                        )
                        rr.raise_for_status()
                        sm_refresh_id = rr.headers.get("RequestId")
                        print(f"      refresh requestId = {sm_refresh_id}")
                    except Exception as e:
                        print(f"      WARN: semantic model refresh failed: {e}")
        except Exception as e:
            print(f"      SKIPPED (pipeline trigger failed): {e}")

    print("\n" + "=" * 60)
    print(f"Done. Open: https://app.powerbi.com/groups/{workspace_id}/list")
    print("\nNext steps:")
    print("  1. Open the 'DWD Wetter-Insights' report (Home page).")
    print("  2. Run 'DWD-Wetter-Insights Forecast Loader' once to populate Mosmix forecasts.")
    print("  3. Schedule the 'DWD-Wetter-Insights Daily' pipeline (e.g., daily 06:00).")

    return {
        "folder_id":         folder_id,
        "lakehouse_id":      lh_id,
        "sql_endpoint_id":   sql_ep_id,
        "loader_nb_id":      loader_nb_id,
        "forecast_nb_id":    forecast_nb_id,
        "refresh_sm_nb_id":  refresh_sm_nb_id,
        "notify_nb_id":      notify_nb_id,
        "semanticmodel_id":  sm_id,
        "report_id":         rpt_id,
        "pipeline_id":       pl_id,
        "pipeline_run_id":   pl_run_id,
        "sm_refresh_id":     sm_refresh_id,
    }


def main():
    ap = argparse.ArgumentParser(description="Deploy DWD-Wetter-Insights to Microsoft Fabric.")
    ap.add_argument("--workspace-id", default=None, help="Target Fabric workspace GUID (or env FABRIC_WORKSPACE_ID)")
    ap.add_argument("--lakehouse-name", default=LAKEHOUSE_NAME, help=f"Lakehouse display name (default: {LAKEHOUSE_NAME})")
    ap.add_argument("--no-run-pipeline", action="store_true", help="Skip the initial pipeline run.")
    ap.add_argument("--no-wait", action="store_true", help="Don't wait for pipeline completion.")
    ap.add_argument("--pipeline-timeout-min", type=int, default=60)
    args = ap.parse_args()
    install(workspace_id=args.workspace_id, lakehouse_name=args.lakehouse_name,
            run_pipeline=not args.no_run_pipeline, wait_for_pipeline=not args.no_wait,
            pipeline_timeout_min=args.pipeline_timeout_min)


if __name__ == "__main__":
    main()
