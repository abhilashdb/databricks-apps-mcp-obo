# MCP OBO Authentication Demo

This project demonstrates how to invoke MCP Services (Google Calendar, Gmail)
with On-Behalf-Of (OBO) authentication in three scenarios:

## Architecture Overview

```
                    SCENARIO 1: Streamlit UI (OBO)
                    ==============================

    User Browser
        |  SSO login
        v
    Databricks Apps Platform
        |  Validates user, mints scoped token
        |  Injects: x-forwarded-access-token
        v
    app.py (Streamlit)
        |  Reads x-forwarded-access-token
        |  WorkspaceClient(token=user_token)
        |  DatabricksMCPClient(ws=user_ws)
        v
    Unity Gateway (/ai-gateway/mcp-services/system.ai.*)
        |  Checks EXECUTE grant
        |  Resolves user's per-user OAuth creds
        v
    Google Calendar / Gmail API
        |  Returns user-scoped data
        v
    Back to Streamlit UI


                    SCENARIO 2: FastAPI /api/ Endpoint (OBO)
                    ========================================

    External Caller (local, CI, other service)
        |  Authorization: Bearer <databricks_token>
        v
    Databricks Apps Platform
        |  Validates Bearer token
        |  Mints scoped OBO token
        |  Injects: x-forwarded-access-token
        v
    api_app.py (FastAPI)
        |  POST /api/mcp/google_calendar/call
        |  Reads x-forwarded-access-token
        |  WorkspaceClient(token=user_token)
        |  DatabricksMCPClient(ws=user_ws)
        v
    Unity Gateway -> Google API -> back to caller


                    SCENARIO 3: Direct from Local (no App)
                    ======================================

    local_client.py
        |  WorkspaceClient(profile="DEFAULT")
        |  Authenticates via OAuth U2M or PAT
        v
    DatabricksMCPClient(ws=local_ws)
        |  Sends user's token directly
        v
    Unity Gateway -> Google API -> back to script
```

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit app - interactive UI with OBO MCP calling (Scenario 1) |
| `api_app.py` | FastAPI app - `/api/` endpoints for external callers (Scenario 2) |
| `local_client.py` | Local script - direct MCP or via App API (Scenario 3) |
| `app.yaml` | App config - declares `user_api_scopes: [ai-gateway]` |
| `requirements.txt` | Python dependencies |

## Key Auth Concepts

### 1. `user_api_scopes` in `app.yaml`

This is the **critical** configuration. Without it, the app only gets service
principal credentials. With `user_api_scopes: [ai-gateway]`, the Databricks
Apps platform will:

- Intercept every request to the app
- Validate the user's identity
- Mint a scoped OAuth token with only the declared scopes
- Inject it as `x-forwarded-access-token` header

### 2. `x-forwarded-access-token` Header

This header carries the user's scoped OBO token. Framework-specific extraction:

```python
# Streamlit
token = st.context.headers.get('x-forwarded-access-token')

# FastAPI / Flask
token = request.headers.get('x-forwarded-access-token')

# Gradio
token = request.headers.get('x-forwarded-access-token')  # via gr.Request
```

### 3. DatabricksMCPClient Authentication Chain

```python
# The MCP client uses the WorkspaceClient's auth for all MCP calls
ws = WorkspaceClient(host=host, token=user_token)  # OBO token
mcp = DatabricksMCPClient(server_url=url, workspace_client=ws)

# Every call_tool() sends the user's token to Unity Gateway
result = mcp.call_tool("calendar_event_list", {})
```

### 4. Per-User OAuth for External Services

MCP Services like `system.ai.google_calendar` use managed OAuth. Each user
must complete a one-time consent flow:

1. Go to Catalog Explorer > `system.ai.google_calendar`
2. Click "Login" to authorize Google Calendar access
3. Databricks stores the OAuth refresh token securely
4. On each MCP call, Unity Gateway exchanges the refresh token for
   an access token and forwards it to Google's API

If the user hasn't consented, the MCP call returns JSON-RPC error `-32042`
with a `login_url` in `error.data.elicitations[]`.

### 5. External Callers (via /api/ endpoint)

For external callers to use OBO through the app:

```bash
# Generate a Databricks token
export TOKEN=$(databricks auth token --host https://<workspace>)

# Call the app's API endpoint
curl -X POST \
  "https://<app-url>/api/mcp/google_calendar/call" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "calendar_event_list", "tool_args": {}}'
```

The Databricks Apps platform validates the Bearer token, mints a scoped
OBO token, and injects it as `x-forwarded-access-token` for the app to use.

## Deployment

### Deploy Streamlit version (Scenario 1)

1. Create a Databricks App from the UI or CLI
2. Deploy with source code path pointing to this folder
3. Ensure `app.yaml` has `user_api_scopes: [ai-gateway]`
4. Users must have completed Google OAuth consent

### Deploy FastAPI version (Scenario 2)

1. Change `app.yaml` command to:
   ```yaml
   command:
     - uvicorn
     - api_app:app
     - --host
     - "0.0.0.0"
   ```
2. Deploy the app
3. External callers can now use `/api/` endpoints with Bearer tokens

### Run locally (Scenario 3)

```bash
pip install databricks-sdk databricks-mcp httpx
databricks auth login --host https://<workspace>

# Direct MCP access
python local_client.py

# Via App API (after deploying api_app.py)
python local_client.py --via-app
```

## Common Pitfalls

These are real issues encountered during development, with solutions:

### 1. `ValueError: more than one authorization method configured: oauth and pat`

**Cause:** The Apps runtime injects service principal OAuth env vars (`DATABRICKS_CLIENT_ID`,
`DATABRICKS_CLIENT_SECRET`). When you also pass `token=user_token` to `WorkspaceClient`, the
SDK detects both "oauth" and "pat" strategies and rejects the ambiguity.

**Fix:** Use `auth_type="pat"` to force PAT-only strategy:
```python
ws = WorkspaceClient(
    host=WORKSPACE_HOST,
    token=user_token,   # OBO token from header
    auth_type="pat",    # ignore SP OAuth env vars
)
```

**Also:** Read `DATABRICKS_HOST` directly from `os.environ` instead of `Config()`,
which would load the full SP auth config and cause the same conflict.

### 2. `Request URL is missing an 'http://' or 'https://' protocol`

**Cause:** `DATABRICKS_HOST` env var in the Apps runtime may be a bare hostname
without the `https://` scheme.

**Fix:**
```python
_raw_host = os.environ["DATABRICKS_HOST"].rstrip("/")
WORKSPACE_HOST = _raw_host if _raw_host.startswith("https://") else f"https://{_raw_host}"
```

### 3. `asyncio.run() cannot be called from a running event loop`

**Cause:** `DatabricksMCPClient` internally uses `asyncio.run()`. In FastAPI/uvicorn,
there's already a running event loop, so nesting fails.

**Fix:** Run the sync MCP calls in a separate thread:
```python
# Before (fails in FastAPI):
result = client.call_tool(tool_name, tool_args)

# After:
result = await asyncio.to_thread(client.call_tool, tool_name, tool_args)
```

This does **not** apply to Streamlit, which doesn't use an async event loop.

### 4. `unhandled errors in a TaskGroup (1 sub-exception)`

**Cause:** `DatabricksMCPClient` wraps errors in Python 3.11 `ExceptionGroup`.
`str(e)` only shows the outer "TaskGroup" message; the real error (e.g., `-32042`
OAuth consent URL) is buried inside.

**Fix:** Recursively unwrap:
```python
def unwrap_exception(e):
    if hasattr(e, 'exceptions') and e.exceptions:
        return unwrap_exception(e.exceptions[0])
    return str(e)
```

### 5. Calendar returns ancient events

**Cause:** Calling `calendar_event_list` with no `time_min`/`time_max` returns
events from the beginning of the calendar.

**Fix:** Always pass RFC3339 time bounds:
```python
result = mcp.call_tool("calendar_event_list", {
    "time_min": "2024-01-01T00:00:00Z",
    "time_max": "2024-01-08T00:00:00Z",
})
```

## Scope Reference

| Scope | Enables |
|---|---|
| `ai-gateway` | MCP Services, AI Gateway endpoints |
| `sql` | SQL warehouse queries |
| `genie` | Genie agents |
| `catalog.tables` | UC table metadata |
| `catalog.connections` | UC connections |
| `files` | File/volume access |
| `model-serving` | Model serving endpoints |

## References

- [Authentication for agents](https://docs.databricks.com/aws/en/agents/custom-agents/agent-authentication)
- [Configure authorization in a Databricks app](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)
- [MCP Services](https://docs.databricks.com/aws/en/agents/mcp-tools/mcp-services)
- [Use MCP servers in Custom Agents](https://docs.databricks.com/aws/en/agents/mcp-tools/use-mcp-in-agents)
- [Connect to an API Databricks app using token authentication](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/connect-local)
- [Databricks-provided MCP Services](https://docs.databricks.com/aws/en/agents/mcp-tools/built-in-mcp-services)

