"""Minimal Power BI Service client: sign in once, list workspaces/models, run DAX.

Sign-in has three modes, picked automatically:
- Your PC (default): opens the browser the first time; the token is cached on this PC
  (Windows-encrypted) so later runs don't ask again.
- Cloud / GitHub Codespaces (no browser on the machine): prints a device code in the
  terminal. Open https://microsoft.com/devicelogin on any device, enter the code, sign in.
  Used when the CODESPACES env var is set, or AUTH_MODE=device.
- Unattended server: if AZURE_CLIENT_ID + AZURE_CLIENT_SECRET (+ TENANT_ID) are set,
  signs in as that service principal, no human needed.
"""
import logging
import os
import sys

import requests
from azure.identity import (
    AuthenticationRecord,
    ClientSecretCredential,
    DeviceCodeCredential,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Keep the Azure sign-in library quiet (it logs every token request at INFO)
logging.getLogger("azure").setLevel(logging.WARNING)

API = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"
RECORD_FILE = os.path.join(os.path.dirname(__file__), ".auth_record.json")

_credential = None


def auth_mode():
    if os.getenv("AZURE_CLIENT_ID") and os.getenv("AZURE_CLIENT_SECRET"):
        return "service_principal"
    if os.getenv("AUTH_MODE", "").lower() == "device" or os.getenv("CODESPACES"):
        return "device"
    return "browser"


def _show_device_code(verification_uri, user_code, expires_on):
    # stderr, not stdout: in the MCP server, stdout is the MCP protocol channel
    print(
        f"\n=== Power BI sign-in needed ===\n"
        f"Open {verification_uri} on any device and enter code: {user_code}\n"
        f"Sign in with the account that owns the report.\n",
        file=sys.stderr, flush=True,
    )


def _get_device_credential(tenant):
    # Linux containers have no keyring, so the token cache is a plain file inside the codespace
    cache = TokenCachePersistenceOptions(name="jhala_ai_analyst", allow_unencrypted_storage=True)
    record = None
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE) as f:
            record = AuthenticationRecord.deserialize(f.read())
    cred = DeviceCodeCredential(
        tenant_id=tenant, cache_persistence_options=cache,
        authentication_record=record, prompt_callback=_show_device_code,
    )
    if record is None:
        record = cred.authenticate(scopes=[SCOPE])
        with open(RECORD_FILE, "w") as f:
            f.write(record.serialize())
    return cred


def sign_in():
    """Sign in up front (cloud modes), so the device code shows at startup, not mid-question."""
    _get_credential().get_token(SCOPE)


def _get_credential():
    global _credential
    if _credential:
        return _credential
    tenant = os.getenv("TENANT_ID") or None
    mode = auth_mode()
    if mode == "service_principal":
        _credential = ClientSecretCredential(
            tenant_id=os.environ["TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
        )
        return _credential
    if mode == "device":
        _credential = _get_device_credential(tenant)
        return _credential
    cache = TokenCachePersistenceOptions(name="jhala_ai_analyst")
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE) as f:
            record = AuthenticationRecord.deserialize(f.read())
        _credential = InteractiveBrowserCredential(
            tenant_id=tenant, cache_persistence_options=cache, authentication_record=record
        )
    else:
        _credential = InteractiveBrowserCredential(
            tenant_id=tenant, cache_persistence_options=cache,
            login_hint=os.getenv("LOGIN_HINT") or None,  # pre-fills the account on the sign-in page
        )
        record = _credential.authenticate(scopes=[SCOPE])
        with open(RECORD_FILE, "w") as f:
            f.write(record.serialize())
    return _credential


def _headers():
    token = _get_credential().get_token(SCOPE).token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_workspaces():
    r = requests.get(f"{API}/groups", headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json()["value"]


def list_models(workspace_id):
    r = requests.get(f"{API}/groups/{workspace_id}/datasets", headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json()["value"]


def get_model_name():
    """Display name of the semantic model in SEMANTIC_MODEL_ID."""
    workspace_id = os.getenv("WORKSPACE_ID", "me")
    base = API if workspace_id in ("", "me") else f"{API}/groups/{workspace_id}"
    r = requests.get(f"{base}/datasets/{os.environ['SEMANTIC_MODEL_ID']}", headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json()["name"]


def get_embed_config():
    """Embed URL + a view-only embed token for REPORT_ID, so visitors see the report without signing in."""
    workspace_id = os.getenv("WORKSPACE_ID", "me")
    report_id = os.environ["REPORT_ID"]
    base = API if workspace_id in ("", "me") else f"{API}/groups/{workspace_id}"
    r = requests.get(f"{base}/reports/{report_id}", headers=_headers(), timeout=60)
    r.raise_for_status()
    report = r.json()
    t = requests.post(f"{base}/reports/{report_id}/GenerateToken", headers=_headers(),
                      json={"accessLevel": "View"}, timeout=60)
    if not t.ok:
        raise RuntimeError(f"Embed token failed ({t.status_code}): {t.text[:300]}")
    tok = t.json()
    return {"reportId": report_id, "embedUrl": report["embedUrl"], "token": tok["token"], "expiration": tok["expiration"]}


def run_dax(dax, workspace_id=None, model_id=None):
    """Run a DAX query on the published semantic model. Returns a list of row dicts."""
    workspace_id = workspace_id or os.getenv("WORKSPACE_ID", "me")
    model_id = model_id or os.environ["SEMANTIC_MODEL_ID"]
    body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
    # "me" (or blank) = My workspace, which has no group ID in the API path
    base = API if workspace_id in ("", "me") else f"{API}/groups/{workspace_id}"
    r = requests.post(
        f"{base}/datasets/{model_id}/executeQueries",
        headers=_headers(), json=body, timeout=120,
    )
    if not r.ok:
        raise RuntimeError(f"DAX failed ({r.status_code}): {r.text[:500]}")
    rows = r.json()["results"][0]["tables"][0]["rows"]
    # Clean "Table[Column]" / "[Measure]" keys to plain names
    return [{k.split("[")[-1].rstrip("]"): v for k, v in row.items()} for row in rows]
