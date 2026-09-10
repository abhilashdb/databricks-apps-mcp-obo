"""FastAPI App: MCP Tool Invocation with OBO via /api/ Endpoint

This is an ALTERNATIVE to the Streamlit app. Deploy this instead when
you need external callers (local scripts, other services) to invoke
MCP tools through the app using token authentication.

=== AUTH FLOW (External Caller -> App /api -> MCP) ===

1. External caller (local machine, another service) obtains a
   Databricks access token (PAT, OAuth U2M, or M2M).
2. Caller sends HTTP request to:
   https://<app-name>-<id>.<region>.databricksapps.com/api/invoke_mcp
   with `Authorization: Bearer <token>` header.
3. Databricks Apps platform validates the token.
   - If app has `user_api_scopes`, the platform mints a
     scoped OBO token and injects `x-forwarded-access-token`.
   - The app reads this header to act as the calling user.
4. App creates WorkspaceClient with the user's token and calls MCP.
5. Unity Gateway proxies to the external service using the user's
   per-user OAuth credentials.

IMPORTANT: /api/ prefix is required for external access.
Routes without /api/ are only accessible through the app's UI.

To deploy this instead of the Streamlit app, change app.yaml command to:
  command:
    - uvicorn
    - api_app:app
    - --host
    - "0.0.0.0"
"""

import asyncio
import json
import os
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from databricks.sdk import WorkspaceClient

try:
    from databricks_mcp import DatabricksMCPClient
    MCP_CLIENT_AVAILABLE = True
except ImportError:
    MCP_CLIENT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Read host directly from env. Do NOT use Config() — it loads SP OAuth creds
# from env vars, which conflicts with the OBO token ("more than one auth method").
_raw_host = os.environ["DATABRICKS_HOST"].rstrip("/")
WORKSPACE_HOST = _raw_host if _raw_host.startswith("https://") else f"https://{_raw_host}"

MCP_SERVICES = {
    "google_calendar": f"{WORKSPACE_HOST}/ai-gateway/mcp-services/system.ai.google_calendar",
    "gmail": f"{WORKSPACE_HOST}/ai-gateway/mcp-services/system.ai.gmail",
}

app = FastAPI(
    title="MCP OBO API",
    description="Invoke MCP tools with On-Behalf-Of authentication",
)


# ---------------------------------------------------------------------------
# Helper: Extract user's OBO token from the request
# ---------------------------------------------------------------------------
def get_user_ws_from_request(request: Request) -> WorkspaceClient:
    """Extract the OBO token from headers and return a user-scoped WorkspaceClient.

    When deployed as a Databricks App with user_api_scopes, the platform
    validates the caller's Bearer token and injects a scoped OBO token
    as `x-forwarded-access-token`.
    """
    user_token = request.headers.get("x-forwarded-access-token")
    if not user_token:
        raise HTTPException(
            status_code=401,
            detail=(
                "No x-forwarded-access-token found. Either:\n"
                "- The app is not configured with user_api_scopes in app.yaml\n"
                "- The caller did not provide a valid Bearer token\n"
                "- You're running locally (not deployed as a Databricks App)"
            ),
        )
    return WorkspaceClient(
        host=WORKSPACE_HOST,
        token=user_token,   # OBO token from the header
        auth_type="pat",    # force PAT-only; ignore SP OAuth env vars
    )


# ---------------------------------------------------------------------------
# API Endpoints (prefixed with /api/ for external access)
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "mcp_client_available": MCP_CLIENT_AVAILABLE}


@app.get("/api/whoami")
async def whoami(request: Request):
    """Returns the identity of the calling user (proves OBO is working)."""
    ws = get_user_ws_from_request(request)
    try:
        me = ws.current_user.me()
        return {
            "user": me.user_name,
            "display_name": me.display_name,
            "auth_type": "on-behalf-of-user",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug/token")
async def debug_token(request: Request):
    """Debug: inspect the OBO token the app receives (safe subset)."""
    import base64
    token = request.headers.get("x-forwarded-access-token", "")
    if not token:
        return {"error": "no x-forwarded-access-token"}

    # Decode JWT claims (middle segment) without verification
    parts = token.split(".")
    info = {"token_parts": len(parts), "token_preview": token[:20] + "..."}
    if len(parts) >= 2:
        try:
            # Add padding for base64
            padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
            # Show safe fields only (scopes, audience, issuer, expiry)
            info["scopes"] = claims.get("scp", claims.get("scope", "N/A"))
            info["aud"] = claims.get("aud", "N/A")
            info["iss"] = claims.get("iss", "N/A")
            info["sub"] = claims.get("sub", "N/A")
            info["azp"] = claims.get("azp", "N/A")  # authorized party
            import time
            exp = claims.get("exp")
            if exp:
                info["expires_in_sec"] = exp - int(time.time())
        except Exception as e:
            info["decode_error"] = str(e)
    return info


@app.get("/api/mcp/{service_name}/tools")
async def list_tools(service_name: str, request: Request):
    """List available tools for an MCP Service."""
    if service_name not in MCP_SERVICES:
        raise HTTPException(404, f"Unknown service: {service_name}. Available: {list(MCP_SERVICES.keys())}")

    ws = get_user_ws_from_request(request)
    client = DatabricksMCPClient(
        server_url=MCP_SERVICES[service_name],
        workspace_client=ws,
    )
    # DatabricksMCPClient.list_tools() internally calls asyncio.run(),
    # which fails inside FastAPI/uvicorn's already-running event loop.
    # Fix: run the sync call in a separate thread.
    tools = await asyncio.to_thread(client.list_tools)
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.inputSchema if hasattr(t, 'inputSchema') else None,
        }
        for t in tools
    ]


@app.post("/api/mcp/{service_name}/call")
async def call_tool(service_name: str, request: Request):
    """Call an MCP tool on behalf of the authenticated user.

    Request body:
    {
        "tool_name": "calendar_event_list",
        "tool_args": {}
    }
    """
    if service_name not in MCP_SERVICES:
        raise HTTPException(404, f"Unknown service: {service_name}")

    body = await request.json()
    tool_name = body.get("tool_name")
    tool_args = body.get("tool_args", {})

    if not tool_name:
        raise HTTPException(400, "tool_name is required")

    ws = get_user_ws_from_request(request)
    client = DatabricksMCPClient(
        server_url=MCP_SERVICES[service_name],
        workspace_client=ws,
    )

    try:
        # Run in thread to avoid "asyncio.run() cannot be called from a running event loop"
        result = await asyncio.to_thread(client.call_tool, tool_name, tool_args)
        if result.content:
            try:
                parsed = json.loads(result.content[0].text)
                return {"result": parsed}
            except (json.JSONDecodeError, AttributeError, IndexError):
                return {"result": [c.text for c in result.content]}
        return {"result": str(result)}
    except Exception as e:
        # Unwrap ExceptionGroup/TaskGroup and extract full detail
        inner = _unwrap_exception_obj(e)
        error_msg = str(inner)

        # Try to extract structured detail from the exception attributes
        # DatabricksMCPClient may attach .data, .code, or nested info
        detail_parts = [error_msg]
        for attr in ('data', 'code', 'message', 'args', '__cause__'):
            val = getattr(inner, attr, None)
            if val and str(val) != error_msg:
                detail_parts.append(f"{attr}={val}")
        full_detail = " | ".join(detail_parts)

        # Log full traceback to app logs for debugging
        import traceback
        traceback.print_exc()

        if "-32042" in full_detail or "elicitation" in full_detail.lower() or "login" in full_detail.lower():
            raise HTTPException(
                403,
                "OAuth consent required. The user must authorize this "
                "MCP Service in Catalog Explorer first (click 'Login'). "
                f"Detail: {full_detail}",
            )
        raise HTTPException(500, f"MCP call failed: {full_detail}")


def _unwrap_exception_obj(e: Exception) -> Exception:
    """Recursively unwrap ExceptionGroup to get the innermost exception object."""
    if hasattr(e, 'exceptions') and e.exceptions:
        return _unwrap_exception_obj(e.exceptions[0])
    if e.__cause__:
        return _unwrap_exception_obj(e.__cause__)
    return e


def _resolve_mcp_url(mcp_uri: str) -> str:
    """Resolve an MCP URI to a full URL.

    Accepts:
      - Full URL: https://host/ai-gateway/mcp-services/catalog.schema.service
      - Fully qualified name: catalog.schema.service
      - Connection name prefixed with 'connection:': connection:my_conn
    """
    if mcp_uri.startswith("https://") or mcp_uri.startswith("http://"):
        return mcp_uri
    if mcp_uri.startswith("connection:"):
        conn_name = mcp_uri[len("connection:"):]
        return f"{WORKSPACE_HOST}/ai-gateway/connections/{conn_name}"
    # Assume fully qualified MCP service name (catalog.schema.service)
    return f"{WORKSPACE_HOST}/ai-gateway/mcp-services/{mcp_uri}"


# ---------------------------------------------------------------------------
# Custom MCP URI endpoints — call ANY MCP service by URI
# ---------------------------------------------------------------------------

@app.post("/api/mcp/custom/tools")
async def custom_list_tools(request: Request):
    """List tools for any MCP service by URI.

    Request body:
    {
        "mcp_uri": "catalog.schema.service_name"
    }

    mcp_uri formats:
      - Fully qualified name: "<your_catalog>.<your_schema>.<your_mcp_service>"
      - Full URL: "https://<host>/ai-gateway/mcp-services/catalog.schema.service"
      - UC connection: "connection:my_slack_conn"
    """
    body = await request.json()
    mcp_uri = body.get("mcp_uri")
    if not mcp_uri:
        raise HTTPException(400, "mcp_uri is required")

    service_url = _resolve_mcp_url(mcp_uri)
    ws = get_user_ws_from_request(request)
    client = DatabricksMCPClient(server_url=service_url, workspace_client=ws)

    try:
        tools = await asyncio.to_thread(client.list_tools)
        return {
            "mcp_uri": mcp_uri,
            "resolved_url": service_url,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema if hasattr(t, 'inputSchema') else None,
                }
                for t in tools
            ],
        }
    except Exception as e:
        inner = _unwrap_exception_obj(e)
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Failed to list tools for {mcp_uri}: {inner}")


@app.post("/api/mcp/custom/call")
async def custom_call_tool(request: Request):
    """Call a tool on any MCP service by URI.

    Request body:
    {
        "mcp_uri": "catalog.schema.service_name",
        "tool_name": "tool_to_call",
        "tool_args": {}
    }
    """
    body = await request.json()
    mcp_uri = body.get("mcp_uri")
    tool_name = body.get("tool_name")
    tool_args = body.get("tool_args", {})

    if not mcp_uri:
        raise HTTPException(400, "mcp_uri is required")
    if not tool_name:
        raise HTTPException(400, "tool_name is required")

    service_url = _resolve_mcp_url(mcp_uri)
    ws = get_user_ws_from_request(request)
    client = DatabricksMCPClient(server_url=service_url, workspace_client=ws)

    try:
        result = await asyncio.to_thread(client.call_tool, tool_name, tool_args)
        if result.content:
            try:
                parsed = json.loads(result.content[0].text)
                return {"mcp_uri": mcp_uri, "tool_name": tool_name, "result": parsed}
            except (json.JSONDecodeError, AttributeError, IndexError):
                return {"mcp_uri": mcp_uri, "tool_name": tool_name, "result": [c.text for c in result.content]}
        return {"mcp_uri": mcp_uri, "tool_name": tool_name, "result": str(result)}
    except Exception as e:
        inner = _unwrap_exception_obj(e)
        error_msg = str(inner)
        detail_parts = [error_msg]
        for attr in ('data', 'code', 'message', 'args', '__cause__'):
            val = getattr(inner, attr, None)
            if val and str(val) != error_msg:
                detail_parts.append(f"{attr}={val}")
        full_detail = " | ".join(detail_parts)
        import traceback
        traceback.print_exc()
        if "-32042" in full_detail or "login" in full_detail.lower():
            raise HTTPException(403, f"OAuth consent required: {full_detail}")
        raise HTTPException(500, f"MCP call failed: {full_detail}")


# ---------------------------------------------------------------------------
# Root: Simple HTML page explaining the API
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
    <head><title>MCP OBO API</title></head>
    <body style="font-family: sans-serif; max-width: 800px; margin: 40px auto;">
    <h1>MCP OBO API</h1>
    <p>This app exposes MCP tools via <code>/api/</code> endpoints with
    On-Behalf-Of authentication.</p>

    <h2>Endpoints</h2>
    <ul>
      <li><code>GET /api/health</code> - Health check</li>
      <li><code>GET /api/whoami</code> - Show authenticated user identity</li>
      <li><code>GET /api/mcp/{service}/tools</code> - List MCP tools</li>
      <li><code>POST /api/mcp/{service}/call</code> - Call an MCP tool</li>
    </ul>
    <p>Services: <code>google_calendar</code>, <code>gmail</code></p>

    <h2>Authentication</h2>
    <p>Include a Databricks Bearer token in the Authorization header:</p>
    <pre>curl -H "Authorization: Bearer $TOKEN" https://&lt;this-app&gt;/api/whoami</pre>

    <p>See <code>/docs</code> for interactive API documentation.</p>
    </body>
    </html>
    """
