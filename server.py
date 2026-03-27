"""
Lightfield MCP Server
Exposes Lightfield CRM data (accounts, contacts, opportunities) as MCP tools
so Claude can pull customer context and route issues to Linear.

Requirements:
  pip install mcp httpx

Usage:
  LIGHTFIELD_API_KEY=sk_lf_... python server.py

Then register https://your-server/mcp in Claude.ai Settings > Connectors.
"""

import asyncio
import hmac
import os

import httpx
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LIGHTFIELD_BASE = "https://api.lightfield.app/v1"
LIGHTFIELD_VERSION = "2026-03-01"
API_KEY = os.environ["LIGHTFIELD_API_KEY"]

# Auth: single hard-coded client_id / client_secret pair.
# Generate secure defaults if not provided, but then they must be set
# explicitly so the operator knows the values.
MCP_CLIENT_ID = os.environ.get("MCP_CLIENT_ID", "")
MCP_CLIENT_SECRET = os.environ.get("MCP_CLIENT_SECRET", "")

if not MCP_CLIENT_ID or not MCP_CLIENT_SECRET:
    raise RuntimeError(
        "MCP_CLIENT_ID and MCP_CLIENT_SECRET env vars are required. "
        "Set them to a shared secret that your MCP client will send as query params."
    )

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Lightfield-Version": LIGHTFIELD_VERSION,
    "Content-Type": "application/json",
}

mcp = FastMCP("lightfield")


# ---------------------------------------------------------------------------
# Auth middleware – checks ?client_id=…&client_secret=… on every request
# ---------------------------------------------------------------------------

class ClientCredentialsAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the correct client_id + client_secret."""

    async def dispatch(self, request: Request, call_next):
        client_id = request.query_params.get("client_id", "")
        client_secret = request.query_params.get("client_secret", "")

        # Constant-time comparison to prevent timing attacks
        id_ok = hmac.compare_digest(client_id, MCP_CLIENT_ID)
        secret_ok = hmac.compare_digest(client_secret, MCP_CLIENT_SECRET)

        if not (id_ok and secret_ok):
            return JSONResponse(
                {"error": "invalid_client", "message": "Invalid client_id or client_secret"},
                status_code=401,
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    """Synchronous GET against the Lightfield API."""
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{LIGHTFIELD_BASE}{path}", headers=HEADERS, params=params or {})
        r.raise_for_status()
        return r.json()


def _flatten_fields(obj: dict) -> dict:
    """
    Lightfield stores custom fields under obj['fields'] as
    {key: {value: ..., valueType: ...}}.
    Flatten these into a simple key→value dict for readability.
    """
    flat = {k: v for k, v in obj.items() if k != "fields"}
    for field_key, field_data in (obj.get("fields") or {}).items():
        flat[field_key] = field_data.get("value")
    return flat


# ---------------------------------------------------------------------------
# Tools: Accounts
# ---------------------------------------------------------------------------

@mcp.tool()
def list_accounts(
    query: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """
    List Lightfield accounts (companies/organizations).
    Use `query` to search by name. Returns flattened account records
    including all custom fields.

    Use this to:
    - Find a customer before routing a Linear issue
    - Browse accounts for pattern analysis
    """
    params = {"limit": limit, "offset": offset}
    if query:
        params["query"] = query
    data = _get("/accounts", params)
    return [_flatten_fields(a) for a in data.get("data", [])]


@mcp.tool()
def get_account(account_id: str) -> dict:
    """
    Retrieve a single Lightfield account by ID.
    Returns the full account record with all custom fields flattened.

    Use this when you have an account ID from list_accounts and need
    complete detail before routing to Linear or analyzing the account.
    """
    data = _get(f"/accounts/{account_id}")
    return _flatten_fields(data)


# ---------------------------------------------------------------------------
# Tools: Contacts
# ---------------------------------------------------------------------------

@mcp.tool()
def list_contacts(
    account_id: str = "",
    query: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """
    List contacts in Lightfield. Optionally filter by account_id to get
    all contacts for a specific company, or search by name/email with query.

    Use this to identify who to reference when creating a Linear issue
    or to understand the people involved with an account.
    """
    params = {"limit": limit, "offset": offset}
    if query:
        params["query"] = query
    if account_id:
        params["accountId"] = account_id
    data = _get("/contacts", params)
    return [_flatten_fields(c) for c in data.get("data", [])]


@mcp.tool()
def get_contact(contact_id: str) -> dict:
    """
    Retrieve a single Lightfield contact by ID.
    """
    data = _get(f"/contacts/{contact_id}")
    return _flatten_fields(data)


# ---------------------------------------------------------------------------
# Tools: Opportunities
# ---------------------------------------------------------------------------

@mcp.tool()
def list_opportunities(
    account_id: str = "",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """
    List opportunities (deals) in Lightfield. Filter by account_id to see
    all deals for a specific company.

    Use this to understand deal history, pipeline stage, and deal value
    when analyzing customer patterns or providing context before routing
    an issue to Linear.
    """
    params = {"limit": limit, "offset": offset}
    if account_id:
        params["accountId"] = account_id
    data = _get("/opportunities", params)
    return [_flatten_fields(o) for o in data.get("data", [])]


@mcp.tool()
def get_opportunity(opportunity_id: str) -> dict:
    """
    Retrieve a single Lightfield opportunity by ID.
    """
    data = _get(f"/opportunities/{opportunity_id}")
    return _flatten_fields(data)


# ---------------------------------------------------------------------------
# Tool: Full customer snapshot (convenience)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_customer_snapshot(account_id: str) -> dict:
    """
    Pull a complete customer snapshot for a given account ID:
    account details, all contacts, and all open opportunities.

    Use this as a single call to load everything Claude needs before
    routing a Linear issue or analyzing a customer's situation.
    """
    account = _flatten_fields(_get(f"/accounts/{account_id}"))

    contacts_resp = _get("/contacts", {"accountId": account_id, "limit": 50})
    contacts = [_flatten_fields(c) for c in contacts_resp.get("data", [])]

    opps_resp = _get("/opportunities", {"accountId": account_id, "limit": 50})
    opportunities = [_flatten_fields(o) for o in opps_resp.get("data", [])]

    return {
        "account": account,
        "contacts": contacts,
        "opportunities": opportunities,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Build the SSE Starlette app from FastMCP, then wrap it with auth.
    port = int(os.environ.get("PORT", 8000))

    app = mcp.sse_app()
    app.add_middleware(ClientCredentialsAuthMiddleware)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())