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

REPO_RAW = "https://raw.githubusercontent.com/KornAlexander/Fabric-Demos/main/Hochschul-Insights/templates"
FABRIC = "https://api.fabric.microsoft.com/v1"
FOLDER_NAME = "Hochschul-Insights"

# Local-or-remote template loader -------------------------------------------------

def load_template(name: str) -> dict:
    """Load a template either from the same folder as this script (local clone)
    or from the GitHub raw URL (one-liner mode)."""
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", name)
    if os.path.isfile(local):
        with open(local, encoding="utf-8") as f:
            return json.load(f)
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

def get_token() -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        sys.exit("ERROR: azure-identity not installed. Run: pip install azure-identity requests")
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return cred.get_token("https://api.fabric.microsoft.com/.default").token


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

def main():
    ap = argparse.ArgumentParser(description="Deploy Hochschul-Insights to Microsoft Fabric.")
    ap.add_argument("--workspace-id", default=os.environ.get("FABRIC_WORKSPACE_ID"),
                    help="Target Fabric workspace GUID (or env FABRIC_WORKSPACE_ID)")
    ap.add_argument("--genesis-token", default=os.environ.get("GENESIS_TOKEN"),
                    help="DESTATIS GENESIS API token (or env GENESIS_TOKEN)")
    args = ap.parse_args()

    if not args.workspace_id:
        sys.exit("ERROR: --workspace-id or env FABRIC_WORKSPACE_ID required.")
    if not args.genesis_token:
        sys.exit("ERROR: --genesis-token or env GENESIS_TOKEN required. "
                 "Register at https://www-genesis.destatis.de/ to get one (free).")

    print(f"Authenticating ...")
    token = get_token()
    fab = Fabric(token, args.workspace_id)

    ws = fab.get_workspace()
    ws_name = ws["displayName"]
    print(f"Workspace: {ws_name} ({args.workspace_id})")

    print(f"\n[1/8] Creating folder '{FOLDER_NAME}' ...")
    folder_id = fab.create_folder(FOLDER_NAME)
    print(f"      folder_id = {folder_id}")

    print(f"\n[2/8] Creating Lakehouse 'WebinarLakehouse' (schemas enabled) ...")
    lh_id, sql_ep_id = fab.create_lakehouse("WebinarLakehouse", folder_id)
    print(f"      lakehouse_id = {lh_id}")
    print(f"      sql_endpoint = {sql_ep_id or '(still provisioning)'}")

    print(f"\n[3/8] Creating loader notebook ...")
    loader_def = substitute(load_template("loader_notebook.json"), {
        "WORKSPACE_ID": args.workspace_id,
        "LAKEHOUSE_ID": lh_id,
        "STALE_LH_ID":  lh_id,           # rebind stale metadata to new LH
        "STALE_WS_ID":  args.workspace_id,
        "GENESIS_TOKEN": args.genesis_token,
    })
    loader_nb_id = fab.create_item("Hochschul-Insights GENESIS Loader", "Notebook", loader_def, folder_id)
    print(f"      loader_nb_id = {loader_nb_id}")

    print(f"\n[4/8] Creating dimensions notebook ...")
    dims_def = substitute(load_template("dims_notebook.json"), {
        "WORKSPACE_ID": args.workspace_id,
        "LAKEHOUSE_ID": lh_id,
        "STALE_LH_ID":  lh_id,
        "STALE_WS_ID":  args.workspace_id,
    })
    dims_nb_id = fab.create_item("Hochschul-Insights GENESIS Dimensions", "Notebook", dims_def, folder_id)
    print(f"      dims_nb_id = {dims_nb_id}")

    print(f"\n[5/8] Creating Direct Lake semantic model 'Hochschule' ...")
    sm_def = substitute(load_template("semantic_model.json"), {
        "WORKSPACE_ID": args.workspace_id,
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

    print(f"\n[7/8] Creating pipeline ...")
    pl_def = substitute(load_template("pipeline.json"), {
        "WORKSPACE_ID":  args.workspace_id,
        "LOADER_NB_ID":  loader_nb_id,
        "DIMS_NB_ID":    dims_nb_id,
    })
    pl_id = fab.create_item("Hochschul-Insights GENESIS Pipeline", "DataPipeline", pl_def, folder_id)
    print(f"      pipeline_id = {pl_id}")

    print(f"\n[8/8] Creating DataAgent 'Hochschul-Stats-Agent' ...")
    try:
        da_def = substitute(load_template("dataagent.json"), {
            "WORKSPACE_ID":     args.workspace_id,
            "LAKEHOUSE_ID":     lh_id,
            "SEMANTICMODEL_ID": sm_id,
        })
        da_id = fab.create_item("Hochschul-Stats-Agent", "DataAgent", da_def, folder_id)
        print(f"      dataagent_id = {da_id}")
    except Exception as e:
        # DataAgent creation via Items API can fail in tenants without preview enabled.
        print(f"      SKIPPED (DataAgent create failed): {e}")
        print(f"      You can recreate it manually from the report's 'Ask a question' tile.")

    print(f"\n{'='*60}")
    print(f"Done. Open: https://app.powerbi.com/groups/{args.workspace_id}/list")
    print(f"\nNext steps:")
    print(f"  1. Run the pipeline once to populate the lakehouse (~5-10 min).")
    print(f"     The Direct Lake model will auto-light up after the first load.")
    print(f"  2. Open 'Webinar Hochschule' report.")
    print(f"  3. Schedule the pipeline weekly (DESTATIS updates monthly at most).")


if __name__ == "__main__":
    main()
