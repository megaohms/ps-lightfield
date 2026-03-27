"""
Lightfield MCP Server
Exposes Lightfield CRM data (accounts, contacts, opportunities) as MCP tools
so Claude can pull customer context and route issues to Linear.

Requirements:
  pip install mcp httpx

Usage:
  LIGHTFIELD_API_KEY=sk_lf_... MCP_CLIENT_ID=... MCP_CLIENT_SECRET=... python server.py

Then register https://your-server/sse in Claude.ai Settings > Connectors.
"""

import os
import secrets
import time

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AccessToken,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LIGHTFIELD_BASE = "https://api.lightfield.app/v1"
LIGHTFIELD_VERSION = "2026-03-01"
API_KEY = os.environ["LIGHTFIELD_API_KEY"]

MCP_CLIENT_ID = os.environ.get("MCP_CLIENT_ID", "")
MCP_CLIENT_SECRET = os.environ.get("MCP_CLIENT_SECRET", "")
SERVER_URL = os.environ.get("MCP_SERVER_URL", "https://ps-lightfield-withered-water-3835.fly.dev")

if not MCP_CLIENT_ID or not MCP_CLIENT_SECRET:
    raise RuntimeError(
        "MCP_CLIENT_ID and MCP_CLIENT_SECRET env vars are required."
    )

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Lightfield-Version": LIGHTFIELD_VERSION,
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Single-user OAuth provider (in-memory, auto-approve)
# ---------------------------------------------------------------------------

class SingleUserOAuthProvider:
    """
    Minimal OAuth 2.0 authorization server for a single hard-coded user.
    No login UI — authorize() auto-approves and redirects immediately.
    """

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        # In-memory stores
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Accept any registration but assign our known client_id/secret
        client_info.client_id = self._client_id
        client_info.client_secret = self._client_secret
        client_info.client_id_issued_at = int(time.time())
        client_info.client_secret_expires_at = 0  # never expires
        self._clients[self._client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # Auto-approve: generate auth code and redirect back immediately
        code = secrets.token_urlsafe(32)  # 256 bits of entropy
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,  # 5 min expiry
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Remove used code (one-time use)
        self._auth_codes.pop(authorization_code.code, None)

        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + 3600,  # 1 hour
            resource=authorization_code.resource,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
        )

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self._refresh_tokens.get(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate tokens
        self._refresh_tokens.pop(refresh_token.token, None)

        access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        use_scopes = scopes or refresh_token.scopes

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=use_scopes,
            expires_at=int(time.time()) + 3600,
        )
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=use_scopes,
        )

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=new_refresh,
            scope=" ".join(use_scopes) if use_scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        at = self._access_tokens.get(token)
        if at and at.expires_at and at.expires_at < time.time():
            self._access_tokens.pop(token, None)
            return None
        return at

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)


# ---------------------------------------------------------------------------
# Build server with OAuth
# ---------------------------------------------------------------------------

oauth_provider = SingleUserOAuthProvider(MCP_CLIENT_ID, MCP_CLIENT_SECRET)

mcp = FastMCP(
    "lightfield",
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=SERVER_URL,
        resource_server_url=SERVER_URL,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["claudeai"],
            default_scopes=["claudeai"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[],
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)


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
    mcp.run(transport="sse")
