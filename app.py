"""Streamlit App: MCP Tool Invocation with On-Behalf-Of (OBO) Authentication

This app demonstrates how a Databricks App can call MCP Services
(e.g., Google Calendar, Gmail) using the logged-in user's identity.

=== AUTH FLOW (OBO inside Databricks Apps) ===

1. User visits this Streamlit app in their browser.
2. Databricks Apps infrastructure authenticates the user via SSO/OAuth.
3. The platform injects the user's scoped OAuth token into the request
   as the HTTP header: `x-forwarded-access-token`.
4. This app reads that header, creates a WorkspaceClient with the
   user's token, and passes it to DatabricksMCPClient.
5. DatabricksMCPClient calls the MCP Service endpoint on Unity Gateway,
   which proxies the call to the external service (Google, etc.)
   using the user's per-user OAuth credentials stored in Databricks.
6. Unity Gateway enforces:
   - EXECUTE grant on the MCP Service securable
   - The user's per-user OAuth consent for the external service
   - Any service policies configured on the MCP Service

PREREQUISITES:
- app.yaml declares `user_api_scopes: ["ai-gateway"]` so the platform
  forwards the user's token with the ai-gateway scope.
- The user has completed the one-time OAuth consent for Google Calendar/Gmail
  by visiting the MCP Service in Catalog Explorer and clicking "Login".
- The user has EXECUTE on system.ai.google_calendar / system.ai.gmail
  (granted by default for account users on system.ai.* services).
"""

import json
import os
import streamlit as st
from databricks.sdk import WorkspaceClient

# --- Attempt to import DatabricksMCPClient; graceful fallback ---
try:
    from databricks_mcp import DatabricksMCPClient
    MCP_CLIENT_AVAILABLE = True
except ImportError:
    MCP_CLIENT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Read ONLY the host from the environment. Do NOT use Config() here because
# the Apps runtime injects both SP OAuth creds (DATABRICKS_CLIENT_ID/SECRET)
# and the user's OBO token (x-forwarded-access-token). Config() picks up
# everything, and later creating WorkspaceClient(token=...) on top of that
# triggers "more than one authorization method configured: oauth and pat".
_raw_host = os.environ["DATABRICKS_HOST"].rstrip("/")
WORKSPACE_HOST = _raw_host if _raw_host.startswith("https://") else f"https://{_raw_host}"

# MCP Service URLs (built-in services via Unity Gateway)
# These use the /ai-gateway/mcp-services/ path -> scope: ai-gateway
MCP_SERVICES = {
    "Google Calendar": f"{WORKSPACE_HOST}/ai-gateway/mcp-services/system.ai.google_calendar",
    "Gmail": f"{WORKSPACE_HOST}/ai-gateway/mcp-services/system.ai.gmail",
    "Web Search (custom)": f"{WORKSPACE_HOST}/ai-gateway/mcp-services/<your_catalog>.<your_schema>.<your_mcp_service>",
}

# MCP via UC HTTP Connections (uses /ai-gateway/connections/ path)
# These route through the UC connections proxy — requires USE_CONNECTION grant
MCP_CONNECTIONS = {
    "Slack (via connection)": f"{WORKSPACE_HOST}/ai-gateway/connections/<your_connection_name>",
}

# ---------------------------------------------------------------------------
# Helper: get a WorkspaceClient authenticated as the visiting user (OBO)
# ---------------------------------------------------------------------------
def get_user_workspace_client() -> tuple:
    """Extract the user's OBO token from Streamlit headers and return
    a WorkspaceClient scoped to that user.

    Returns:
        (WorkspaceClient, user_email_or_none, token_preview)
    """
    # Databricks Apps injects the user's scoped token here:
    user_token = st.context.headers.get("x-forwarded-access-token")

    if not user_token:
        return None, None, None

    # Create a WorkspaceClient that authenticates ONLY with the user's token.
    #
    # The Apps runtime injects SP OAuth env vars (DATABRICKS_CLIENT_ID,
    # DATABRICKS_CLIENT_SECRET, etc.). When we also pass token=, the SDK
    # detects both "oauth" and "pat" strategies and raises:
    #   "more than one authorization method configured: oauth and pat"
    #
    # Fix: auth_type="pat" forces the SDK to use ONLY the PAT credential
    # strategy, ignoring all other env-var-based strategies (OAuth, etc.).
    ws = WorkspaceClient(
        host=WORKSPACE_HOST,
        token=user_token,   # <-- the OBO token from the header
        auth_type="pat",    # <-- force PAT-only; ignore SP OAuth env vars
    )

    # Optionally fetch user identity for display
    try:
        me = ws.current_user.me()
        user_email = me.user_name
    except Exception:
        user_email = "(could not fetch)"

    token_preview = user_token[:12] + "..." + user_token[-4:]
    return ws, user_email, token_preview


# ---------------------------------------------------------------------------
# Helper: unwrap ExceptionGroup / TaskGroup errors
# ---------------------------------------------------------------------------
def unwrap_exception(e: Exception) -> str:
    """Recursively unwrap ExceptionGroup/TaskGroup to get the real error.

    DatabricksMCPClient uses async internally. When an MCP call fails
    (e.g., OAuth consent needed, -32042), the error gets wrapped in a
    Python 3.11 ExceptionGroup ("unhandled errors in a TaskGroup").
    str(e) only shows the outer message; the real error with the login
    URL is buried in e.exceptions[0].
    """
    # Unwrap ExceptionGroup / BaseExceptionGroup
    if hasattr(e, 'exceptions') and e.exceptions:
        # Recurse into the first sub-exception
        return unwrap_exception(e.exceptions[0])
    return str(e)


def format_mcp_error(e: Exception) -> tuple:
    """Return (is_consent_error: bool, display_message: str)."""
    msg = unwrap_exception(e)
    is_consent = "-32042" in msg or "elicitation" in msg.lower() or "login" in msg.lower()
    return is_consent, msg


# ---------------------------------------------------------------------------
# Helper: call an MCP tool
# ---------------------------------------------------------------------------
def call_mcp_tool(ws: WorkspaceClient, service_url: str, tool_name: str, tool_args: dict) -> dict:
    """Call a single MCP tool using the user's WorkspaceClient."""
    client = DatabricksMCPClient(
        server_url=service_url,
        workspace_client=ws,
    )
    result = client.call_tool(tool_name, tool_args)
    # Parse the result - first content block is typically JSON text
    if result.content:
        try:
            return json.loads(result.content[0].text)
        except (json.JSONDecodeError, AttributeError, IndexError):
            return {"raw": [c.text for c in result.content]}
    return {"raw": str(result)}


def list_mcp_tools(ws: WorkspaceClient, service_url: str) -> list:
    """List available tools from an MCP Service."""
    client = DatabricksMCPClient(
        server_url=service_url,
        workspace_client=ws,
    )
    return client.list_tools()


# ===========================================================================
# Streamlit UI
# ===========================================================================
st.set_page_config(page_title="MCP OBO Demo", page_icon="\U0001f511", layout="wide")
st.title("\U0001f511 MCP Tool Invocation with OBO Auth")

# --- Sidebar: Auth Flow Diagrams ---
with st.sidebar:
    st.header("Auth Flows")

    flow_tab1, flow_tab2 = st.tabs(["Browser (Streamlit)", "API (curl / external)"])

    with flow_tab1:
        st.markdown("#### Scenario 1: Browser UI")
        st.code(
            "Browser (User)\n"
            "    |\n"
            "    | 1. SSO login\n"
            "    v\n"
            "Databricks Apps Platform\n"
            "    |\n"
            "    | 2. Validates SSO session\n"
            "    | 3. Mints scoped OBO token\n"
            "    |    (scopes from app.yaml\n"
            "    |     user_api_scopes)\n"
            "    | 4. Injects as header:\n"
            "    |    x-forwarded-access-token\n"
            "    v\n"
            "Streamlit App\n"
            "    |\n"
            "    | 5. st.context.headers.get(\n"
            "    |      'x-forwarded-access-token')\n"
            "    | 6. WorkspaceClient(\n"
            "    |      token=user_token,\n"
            "    |      auth_type='pat')\n"
            "    | 7. DatabricksMCPClient(ws=ws)\n"
            "    v\n"
            "Unity Gateway\n"
            "    |\n"
            "    | 8. Validates EXECUTE grant\n"
            "    | 9. Resolves user's per-user\n"
            "    |    Google OAuth creds\n"
            "    v\n"
            "Google Calendar / Gmail API",
            language=None,
        )

    with flow_tab2:
        st.markdown("#### Scenario 2: External Caller")
        st.code(
            "Local machine / CI / other service\n"
            "    |\n"
            "    | 1. Generate Databricks token:\n"
            "    |    databricks auth token \\\n"
            "    |      --host <workspace>\n"
            "    | 2. Send request with:\n"
            "    |    Authorization: Bearer <token>\n"
            "    v\n"
            "Databricks Apps Platform\n"
            "    |\n"
            "    | 3. Validates Bearer token\n"
            "    | 4. Mints scoped OBO token\n"
            "    |    (same scopes as browser)\n"
            "    | 5. Injects as header:\n"
            "    |    x-forwarded-access-token\n"
            "    v\n"
            "FastAPI App (/api/ routes)\n"
            "    |\n"
            "    | 6. request.headers.get(\n"
            "    |      'x-forwarded-access-token')\n"
            "    | 7. WorkspaceClient(\n"
            "    |      token=user_token,\n"
            "    |      auth_type='pat')\n"
            "    | 8. DatabricksMCPClient(ws=ws)\n"
            "    v\n"
            "Unity Gateway -> Google API\n"
            "    |\n"
            "    v\n"
            "JSON response back to caller",
            language=None,
        )
        st.markdown("""
        **curl example:**
        ```bash
        APP=https://<app-url>
        TOKEN=$(databricks auth token \\
          --host <workspace> | jq -r .access_token)

        # Verify identity
        curl -H "Authorization: Bearer $TOKEN" \\
          "$APP/api/whoami"

        # Call MCP tool
        curl -X POST \\
          "$APP/api/mcp/google_calendar/call" \\
          -H "Authorization: Bearer $TOKEN" \\
          -H "Content-Type: application/json" \\
          -d '{"tool_name": "calendar_event_list",
               "tool_args": {"time_min": "...",
                             "time_max": "..."}}')
        ```
        """)

    st.divider()
    st.markdown("""
    **Key gotchas we hit:**
    - `auth_type="pat"` required on `WorkspaceClient` — Apps runtime injects SP OAuth env vars that conflict
    - `DATABRICKS_HOST` may lack `https://` prefix
    - `DatabricksMCPClient` uses `asyncio.run()` internally — use `asyncio.to_thread()` in FastAPI
    - Errors wrapped in `ExceptionGroup` — unwrap to get real `-32042` / `-32603` codes
    """)

# --- Step 1: Show token extraction ---
st.header("Step 1: Extract OBO Token")

ws, user_email, token_preview = get_user_workspace_client()

if ws is None:
    st.warning(
        "No `x-forwarded-access-token` found in headers. "
        "This means either:\n"
        "- You're running locally (not deployed as a Databricks App)\n"
        "- User authorization scopes aren't configured in app.yaml\n\n"
        "**Deploy this app with `user_api_scopes: [ai-gateway]` to see OBO in action.**"
    )
    st.code(
        '# In Streamlit, the OBO token is extracted like this:\n'
        'user_token = st.context.headers.get("x-forwarded-access-token")\n\n'
        '# Then create a user-scoped WorkspaceClient:\n'
        'ws = WorkspaceClient(host=WORKSPACE_HOST, token=user_token)',
        language="python",
    )
    st.stop()

col1, col2 = st.columns(2)
with col1:
    st.success(f"Authenticated as: **{user_email}**")
with col2:
    st.info(f"Token: `{token_preview}`")

st.code(
    f'# Token extracted from header:\n'
    f'user_token = st.context.headers.get("x-forwarded-access-token")\n'
    f'# => {token_preview}\n\n'
    f'# WorkspaceClient created with user\'s identity:\n'
    f'ws = WorkspaceClient(host="{WORKSPACE_HOST}", token=user_token)\n'
    f'# => Acting as: {user_email}',
    language="python",
)

# --- Step 2: Select MCP Service ---
st.header("Step 2: Select MCP Service & Discover Tools")

if not MCP_CLIENT_AVAILABLE:
    st.error(
        "`databricks-mcp` package not installed. "
        "Add it to requirements.txt and redeploy."
    )
    st.stop()

# Combine both MCP Services and UC Connections into one selector
all_mcp_endpoints = {}
for name, url in MCP_SERVICES.items():
    all_mcp_endpoints[f"MCP Service: {name}"] = (url, "mcp-service")
for name, url in MCP_CONNECTIONS.items():
    all_mcp_endpoints[f"UC Connection: {name}"] = (url, "connection")

selected_label = st.selectbox("MCP Endpoint", list(all_mcp_endpoints.keys()))
service_url, endpoint_type = all_mcp_endpoints[selected_label]

if endpoint_type == "connection":
    st.info(
        "**UC Connection path** (`/ai-gateway/connections/...`): "
        "Routes through the UC HTTP connections proxy. "
        "May require additional OAuth scopes beyond `ai-gateway`."
    )

st.code(
    f'# Endpoint type: {endpoint_type}\n'
    f'mcp_client = DatabricksMCPClient(\n'
    f'    server_url="{service_url}",\n'
    f'    workspace_client=ws,  # user-scoped OBO client\n'
    f')',
    language="python",
)

# --- Discover tools ---
col_discover, col_raw = st.columns(2)

with col_discover:
    discover_btn = st.button("List Available Tools")
with col_raw:
    raw_test_btn = st.button("Raw HTTP Test (show full error)")

if raw_test_btn:
    # Send a raw JSON-RPC initialize request using the OBO token
    # This bypasses the MCP client and shows the exact server response
    import requests as _req
    user_token = st.context.headers.get("x-forwarded-access-token")
    raw_headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "obo-test", "version": "1.0"}
        }
    }
    with st.spinner("Sending raw JSON-RPC initialize..."):
        try:
            resp = _req.post(service_url, json=init_payload, headers=raw_headers, timeout=15)
            st.markdown(f"**HTTP Status:** `{resp.status_code}`")
            st.markdown(f"**Content-Type:** `{resp.headers.get('content-type', 'N/A')}`")
            try:
                st.json(resp.json())
            except Exception:
                st.code(resp.text[:2000])
        except Exception as e:
            st.error(f"Request failed: {e}")

if discover_btn:
    try:
        with st.spinner("Discovering tools via MCP tools/list..."):
            tools = list_mcp_tools(ws, service_url)
        st.session_state["tools"] = tools
        st.success(f"Found {len(tools)} tools")
    except Exception as e:
        is_consent, error_msg = format_mcp_error(e)
        if is_consent:
            st.error(
                "**OAuth consent required.** You haven't authorized this "
                "service yet.\n\n"
                "**To fix:** Open the MCP Service in Catalog Explorer "
                "and click **Login** to complete the one-time OAuth flow.\n\n"
                f"Error detail: `{error_msg}`"
            )
        else:
            st.error(f"Error listing tools: {error_msg}")

if "tools" in st.session_state:
    tools = st.session_state["tools"]
    for tool in tools:
        with st.expander(f"\U0001f527 {tool.name}"):
            st.markdown(f"**Description:** {tool.description or 'N/A'}")
            if hasattr(tool, 'inputSchema') and tool.inputSchema:
                st.json(tool.inputSchema)

# --- Step 3: Call a tool ---
st.header("Step 3: Call an MCP Tool")

# Build tool name list from discovered tools, or let user type
tool_names = [t.name for t in st.session_state.get("tools", [])]
tool_name = st.selectbox(
    "Tool name",
    tool_names if tool_names else ["(discover tools first)"],
)

# Show default args based on known tools
default_args = "{}"
if tool_name == "calendar_event_list":
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    t_min = now.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    t_max = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    default_args = json.dumps({"time_min": t_min, "time_max": t_max}, indent=2)
elif tool_name == "gmail_search":
    default_args = json.dumps({"query": "is:unread", "maxResults": 5}, indent=2)

tool_args_str = st.text_area("Tool arguments (JSON)", value=default_args, height=100)

if st.button("Call Tool"):
    try:
        tool_args = json.loads(tool_args_str)
    except json.JSONDecodeError as e:
        st.error(f"Invalid JSON: {e}")
        st.stop()

    try:
        with st.spinner(f"Calling {tool_name} via MCP..."):
            result = call_mcp_tool(ws, service_url, tool_name=tool_name, tool_args=tool_args)
        st.json(result)
    except Exception as e:
        is_consent, msg = format_mcp_error(e)
        if is_consent:
            st.error(f"**OAuth consent required.** Authorize in Catalog Explorer first.\n\nDetail: `{msg}`")
        else:
            st.error(f"Tool call failed: {msg}")

# --- Auth explanation footer ---
st.divider()
st.header("How It Works")

tab_browser, tab_api, tab_config, tab_pitfalls = st.tabs(
    ["Browser Flow", "API Flow", "Configuration", "Pitfalls"]
)

with tab_browser:
    st.markdown("""
    | Layer | What Happens |
    |---|---|
    | **Browser** | User authenticates via Databricks SSO |
    | **Apps Platform** | Validates SSO, mints scoped OBO token (scopes from `app.yaml`), injects as `x-forwarded-access-token` header |
    | **Streamlit App** | `st.context.headers.get('x-forwarded-access-token')` to read token |
    | **WorkspaceClient** | `WorkspaceClient(host=..., token=user_token, auth_type='pat')` |
    | **DatabricksMCPClient** | `DatabricksMCPClient(server_url=..., workspace_client=ws)` sends user's token to Unity Gateway |
    | **Unity Gateway** | Checks EXECUTE grant on MCP Service, resolves user's per-user OAuth creds for Google |
    | **Google API** | Returns data scoped to the user's Google account |
    """)

with tab_api:
    st.markdown("""
    | Layer | What Happens |
    |---|---|
    | **External Caller** | Generates a Databricks token: `databricks auth token --host <workspace>` |
    | **HTTP Request** | `curl -H "Authorization: Bearer $TOKEN" https://<app>/api/...` |
    | **Apps Platform** | Validates Bearer token, mints scoped OBO token (same mechanism as browser), injects `x-forwarded-access-token` |
    | **FastAPI App** | `request.headers.get('x-forwarded-access-token')` to read token |
    | **WorkspaceClient** | Same as browser: `WorkspaceClient(token=..., auth_type='pat')` |
    | **DatabricksMCPClient** | Must use `await asyncio.to_thread(client.call_tool, ...)` in FastAPI (async event loop conflict) |
    | **Unity Gateway** | Same chain as browser — checks grants, resolves per-user OAuth |

    **Important:** Only `/api/`-prefixed routes are accessible to external callers. Non-`/api/` routes are UI-only.
    """)

with tab_config:
    st.markdown("""
    **`app.yaml` — the critical piece:**
    ```yaml
    user_api_scopes:
      - ai-gateway   # enables OBO token forwarding
    ```
    Without `user_api_scopes`, the platform only provides service principal credentials.
    With it, every request gets a user-scoped `x-forwarded-access-token`.

    **Available scopes:**
    | Scope | Enables |
    |---|---|
    | `ai-gateway` | MCP Services, AI Gateway endpoints |
    | `sql` | SQL warehouse queries |
    | `genie` | Genie agents |
    | `catalog.tables` | UC table metadata |
    | `files` | File / volume access |
    | `model-serving` | Model serving endpoints |

    **Per-user OAuth consent:**
    MCP Services like `system.ai.google_calendar` require each user to
    authorize once: Catalog Explorer > `system.ai.google_calendar` > **Login**.
    Unity Gateway stores the refresh token and handles exchanges transparently.
    """)

with tab_pitfalls:
    st.markdown("""
    | Pitfall | Symptom | Fix |
    |---|---|---|
    | SDK detects both SP OAuth + OBO token | `more than one authorization method: oauth and pat` | `auth_type="pat"` on `WorkspaceClient` |
    | `DATABRICKS_HOST` has no scheme | `URL missing http://` | Prefix `https://` if missing |
    | Nested asyncio loops | `asyncio.run() cannot be called from running loop` | `await asyncio.to_thread(client.call_tool, ...)` |
    | Errors wrapped in ExceptionGroup | `unhandled errors in a TaskGroup` | Unwrap `e.exceptions[0]` recursively |
    | No time bounds on calendar call | Returns ancient events | Pass `time_min`/`time_max` RFC3339 |
    | User hasn't consented to Google | `-32042` with login URL | Visit MCP Service in Catalog Explorer, click Login |
    """)
