"""Publish the Blinkit Pulse PBIP (powerbi/) to a Power BI workspace, then load its data.

1. Creates or updates the semantic model and the report through the Fabric REST API.
2. Sets the data source (public CSVs in this GitHub repo's data/ folder) to anonymous access.
3. Refreshes the model and waits for it to finish.

Uses the service principal in .env (TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, WORKSPACE_ID).
The workspace needs the setting "Service principals can call Fabric public APIs" and the app as a Member.

Run:  .venv/Scripts/python scripts/publish_to_powerbi.py
Prints the SEMANTIC_MODEL_ID and REPORT_ID to put in .env and render.yaml.
"""
import base64
import json
import os
import sys
import time

import requests
from azure.identity import ClientSecretCredential
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, os.getenv("ENV_FILE", ".env")))

NAME = "Blinkit Pulse"
MODEL_DIR = os.path.join(ROOT, "powerbi", "BlinkitPulse.SemanticModel")
REPORT_DIR = os.path.join(ROOT, "powerbi", "BlinkitPulse.Report")
FABRIC = "https://api.fabric.microsoft.com/v1"
PBI = "https://api.powerbi.com/v1.0/myorg"

cred = ClientSecretCredential(os.environ["TENANT_ID"], os.environ["AZURE_CLIENT_ID"], os.environ["AZURE_CLIENT_SECRET"])
WS = os.environ["WORKSPACE_ID"]


def headers(scope):
    return {"Authorization": "Bearer " + cred.get_token(scope).token, "Content-Type": "application/json"}


FH = lambda: headers("https://api.fabric.microsoft.com/.default")
PH = lambda: headers("https://analysis.windows.net/powerbi/api/.default")


def parts(folder, override=None):
    out = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".pbi"]
        for f in files:
            if f in (".platform",) or f.endswith(".abf"):
                continue
            rel = os.path.relpath(os.path.join(base, f), folder).replace("\\", "/")
            data = (override or {}).get(rel)
            if data is None:
                with open(os.path.join(base, f), "rb") as fh:
                    data = fh.read()
            out.append({"path": rel, "payload": base64.b64encode(data).decode(), "payloadType": "InlineBase64"})
    return out


def wait_lro(r):
    """Fabric create/update calls may answer 202 and finish in the background."""
    if r.status_code not in (200, 201, 202):
        sys.exit(f"Fabric API error {r.status_code}: {r.text[:800]}")
    if r.status_code != 202:
        return r.json() if r.text else {}
    url = r.headers["Location"]
    while True:
        time.sleep(int(r.headers.get("Retry-After", 3)))
        r = requests.get(url, headers=FH())
        st = r.json().get("status")
        if st in ("Succeeded", "Failed"):
            if st == "Failed":
                sys.exit(f"Operation failed: {r.text[:800]}")
            res = requests.get(url + "/result", headers=FH())
            return res.json() if res.ok and res.text else {}


def upsert(kind, definition):
    items = requests.get(f"{FABRIC}/workspaces/{WS}/{kind}", headers=FH()).json().get("value", [])
    existing = next((i for i in items if i["displayName"] == NAME), None)
    if existing:
        print(f"Updating {kind[:-1]} {existing['id']}")
        wait_lro(requests.post(f"{FABRIC}/workspaces/{WS}/{kind}/{existing['id']}/updateDefinition",
                               headers=FH(), json={"definition": definition}))
        return existing["id"]
    print(f"Creating {kind[:-1]}")
    wait_lro(requests.post(f"{FABRIC}/workspaces/{WS}/{kind}", headers=FH(),
                           json={"displayName": NAME, "definition": definition}))
    items = requests.get(f"{FABRIC}/workspaces/{WS}/{kind}", headers=FH()).json()["value"]
    return next(i["id"] for i in items if i["displayName"] == NAME)


def main():
    model_id = upsert("semanticModels", {"parts": parts(MODEL_DIR)})
    pbir = json.dumps({"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
                       "version": "4.0", "datasetReference": {
        "byConnection": {"connectionString": f"semanticmodelid={model_id}"}}}).encode()
    report_id = upsert("reports", {"parts": parts(REPORT_DIR, {"definition.pbir": pbir})})

    # Data source: public web CSVs -> anonymous credentials
    srcs = requests.get(f"{PBI}/groups/{WS}/datasets/{model_id}/datasources", headers=PH()).json().get("value", [])
    for s in srcs:
        r = requests.patch(f"{PBI}/gateways/{s['gatewayId']}/datasources/{s['datasourceId']}", headers=PH(), json={
            "credentialDetails": {"credentialType": "Anonymous", "credentials": '{"credentialData":""}',
                                  "encryptedConnection": "Encrypted", "encryptionAlgorithm": "None",
                                  "privacyLevel": "Public"}})
        print("Data source", s.get("connectionDetails"), "->", "anonymous OK" if r.ok else r.text[:300])

    print("Refreshing the model...")
    r = requests.post(f"{PBI}/groups/{WS}/datasets/{model_id}/refreshes", headers=PH(), json={})
    if not r.ok:
        sys.exit(f"Refresh failed to start {r.status_code}: {r.text[:500]}")
    while True:
        time.sleep(5)
        h = requests.get(f"{PBI}/groups/{WS}/datasets/{model_id}/refreshes?$top=1", headers=PH()).json()["value"][0]
        if h["status"] != "Unknown":
            print("Refresh", h["status"], h.get("serviceExceptionJson", ""))
            break
    print(f"\nSEMANTIC_MODEL_ID={model_id}\nREPORT_ID={report_id}")


if __name__ == "__main__":
    main()
