# Lightfield MCP Server

A lightweight MCP server that wraps the Lightfield CRM API so Claude can pull
customer context (accounts, contacts, opportunities) and route issues to Linear.

## Setup

### 1. Install dependencies

```bash
pip install mcp httpx
```

### 2. Set your API key

Create a Lightfield API key at https://crm.lightfield.app/crm/settings/api-keys
with at minimum these scopes: `accounts:read`, `contacts:read`, `opportunities:read`

```bash
export LIGHTFIELD_API_KEY=sk_lf_your_key_here
```

### 3. Run the server

```bash
python server.py
```

The server starts on port 8000 by default. Set `PORT` to override:

```bash
PORT=9000 python server.py
```

### 4. Deploy

Deploy to any server or serverless platform that keeps a persistent process
(Railway, Fly.io, Render, AWS App Runner, etc.).

The server needs to be reachable over HTTPS. Most platforms handle TLS for you.

### 5. Register in Claude.ai

Go to **Claude.ai → Settings → Connectors → Add custom connector**
and enter your server's public URL, e.g.:

```
https://your-server.fly.dev/mcp
```

---

## Tools exposed to Claude

| Tool | Description |
|------|-------------|
| `list_accounts` | Search/browse accounts by name |
| `get_account` | Full account detail by ID |
| `list_contacts` | Contacts, optionally filtered by account |
| `get_contact` | Single contact detail |
| `list_opportunities` | Deals, optionally filtered by account |
| `get_opportunity` | Single opportunity detail |
| `get_customer_snapshot` | All-in-one: account + contacts + opportunities |

## Workflow with Linear

Claude uses this MCP server for **Lightfield context**, then uses its built-in
**Linear connector** for issue routing. No Linear integration needed here.

Example Claude workflow:
1. You ask: *"Route the energy modeling bug to Linear for Acme Corp"*
2. Claude calls `list_accounts(query="Acme")` → gets account ID
3. Claude calls `get_customer_snapshot(account_id=...)` → full context
4. Claude uses Linear connector to create issue + customer need with context attached