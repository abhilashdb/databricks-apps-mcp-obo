"""Local Client: Invoke MCP Tools from Your Machine

This script demonstrates TWO ways to call MCP tools from outside
Databricks Apps:

Approach A: Call MCP directly (no App intermediary)
  - You authenticate to Databricks directly (OAuth U2M or PAT)
  - You call the MCP Service endpoint directly on Unity Gateway
  - Simplest if you just need MCP access from your machine

Approach B: Call via App's /api/ endpoint (agent-hosted scenario)
  - Deploy the FastAPI app (api_app.py) as a Databricks App
  - From local, send your Bearer token to the app's /api/ endpoint
  - The app extracts your OBO token and calls MCP on your behalf
  - Use this when your agent is hosted on Apps and you want external
    callers to invoke it

PREREQUISITES:
  pip install databricks-sdk databricks-mcp httpx

AUTH SETUP (for both approaches):
  Option 1 - Databricks CLI profile:
    databricks auth login --host https://<workspace>.cloud.databricks.com
    Then use WorkspaceClient(profile="DEFAULT")

  Option 2 - Environment variables:
    export DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
    export DATABRICKS_TOKEN=dapi...
    Then use WorkspaceClient()

  Option 3 - OAuth M2M (service principal):
    export DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
    export DATABRICKS_CLIENT_ID=...
    export DATABRICKS_CLIENT_SECRET=...
    Then use WorkspaceClient()
"""

import json
import sys


# ===========================================================================
# APPROACH A: Call MCP directly from local (no App needed)
# ===========================================================================
def approach_a_direct_mcp():
    """Call MCP Services directly from your local machine.

    Auth flow:
    1. WorkspaceClient authenticates you to Databricks (OAuth/PAT)
    2. DatabricksMCPClient sends your token to Unity Gateway
    3. Unity Gateway checks EXECUTE grant + looks up your per-user
       OAuth creds for the external service
    4. External service returns data scoped to your account

    This is the simplest path when you don't need an App intermediary.
    """
    from databricks.sdk import WorkspaceClient
    from databricks_mcp import DatabricksMCPClient

    # Authenticate - SDK auto-detects from env vars or CLI profile
    ws = WorkspaceClient()  # or WorkspaceClient(profile="DEFAULT")
    host = ws.config.host.rstrip("/")

    print(f"Workspace: {host}")
    print(f"Authenticated as: {ws.current_user.me().user_name}")

    # Google Calendar MCP Service URL
    service_url = f"{host}/ai-gateway/mcp-services/system.ai.google_calendar"

    # Create MCP client with your credentials
    mcp = DatabricksMCPClient(
        server_url=service_url,
        workspace_client=ws,
    )

    # List available tools
    print("\n--- Available Tools ---")
    tools = mcp.list_tools()
    for t in tools:
        print(f"  {t.name}: {t.description[:80] if t.description else 'N/A'}")

    # Call a tool
    print("\n--- Calling calendar_event_list ---")
    result = mcp.call_tool("calendar_event_list", {})
    if result.content:
        try:
            data = json.loads(result.content[0].text)
            print(json.dumps(data, indent=2)[:2000])
        except json.JSONDecodeError:
            print(result.content[0].text[:2000])


# ===========================================================================
# APPROACH B: Call MCP via the App's /api/ endpoint
# ===========================================================================
def approach_b_via_app_api():
    """Call MCP tools through the deployed FastAPI app's /api/ endpoint.

    Auth flow:
    1. You generate a Databricks Bearer token locally
    2. You send it to the app's /api/ endpoint
    3. Databricks Apps platform validates your token
    4. Platform mints a scoped OBO token (x-forwarded-access-token)
    5. The app reads the OBO token, creates WorkspaceClient
    6. App calls MCP on your behalf
    7. Results flow back to you

    Use this when your agent is hosted on Apps and needs to be
    callable from external systems.
    """
    import httpx
    from databricks.sdk import WorkspaceClient

    # --- Configuration ---
    # Replace with your actual app URL after deployment
    APP_URL = "https://<your-app-name>-<hash>.<region>.databricksapps.com"

    # Generate a Bearer token from your Databricks credentials
    ws = WorkspaceClient()  # auto-detects auth
    headers = ws.config.authenticate()
    # headers is a callable that returns {"Authorization": "Bearer <token>"}
    auth_headers = headers if isinstance(headers, dict) else {}

    # If headers is a callable (newer SDK), call it
    if callable(headers):
        auth_headers = {}
        headers(auth_headers)  # mutates the dict

    print(f"Auth headers: { {k: v[:20]+'...' for k,v in auth_headers.items()} }")

    # --- Call /api/whoami to verify OBO works ---
    print("\n--- GET /api/whoami ---")
    resp = httpx.get(f"{APP_URL}/api/whoami", headers=auth_headers)
    print(f"Status: {resp.status_code}")
    print(f"Body: {resp.json()}")

    # --- List tools ---
    print("\n--- GET /api/mcp/google_calendar/tools ---")
    resp = httpx.get(
        f"{APP_URL}/api/mcp/google_calendar/tools",
        headers=auth_headers,
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        tools = resp.json()
        for t in tools:
            print(f"  {t['name']}: {t.get('description', 'N/A')[:80]}")

    # --- Call a tool ---
    print("\n--- POST /api/mcp/google_calendar/call ---")
    resp = httpx.post(
        f"{APP_URL}/api/mcp/google_calendar/call",
        headers=auth_headers,
        json={
            "tool_name": "calendar_event_list",
            "tool_args": {},
        },
    )
    print(f"Status: {resp.status_code}")
    print(json.dumps(resp.json(), indent=2)[:2000])


# ===========================================================================
# Main
# ===========================================================================
if __name__ == "__main__":
    print("="*60)
    print("MCP OBO Local Client")
    print("="*60)

    if len(sys.argv) > 1 and sys.argv[1] == "--via-app":
        print("\nApproach B: Calling MCP via App's /api/ endpoint")
        print("-" * 60)
        approach_b_via_app_api()
    else:
        print("\nApproach A: Calling MCP directly (no App intermediary)")
        print("-" * 60)
        print("(Use --via-app flag for Approach B)")
        approach_a_direct_mcp()
