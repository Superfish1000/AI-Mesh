"""
AI Mesh MCP Client
Connects a Claude Code (or any MCP-capable) instance to the AI Mesh server.

Usage:
  python mcp_client.py                         # stdio MCP server (default)
  AI_MESH_URL=http://host:8000 python mcp_client.py

Add to Claude Code (.claude/mcp_servers.json):
  {
    "ai-mesh": {
      "command": "python",
      "args": ["/path/to/mcp_client.py"],
      "env": { "AI_MESH_URL": "http://your-server:8000" }
    }
  }
"""

import asyncio
import json
import os
import platform
import socket
import time
from pathlib import Path
from typing import Optional

import httpx
import websockets
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SERVER_URL: str = os.environ.get("AI_MESH_URL", "http://localhost:8000").rstrip("/")
CONFIG_FILE: Path = Path.home() / ".ai-mesh" / "config.json"
INCOMING_FILE: Path = Path.home() / ".ai-mesh" / "incoming.json"

# Key used to isolate this instance's config from others on the same machine.
# Uses cwd so each project directory gets its own identity. Two Claude Code
# sessions in the same directory share an identity (intentional — same project).
_CWD_KEY: str = str(Path.cwd())

mcp = FastMCP("AI Mesh")

# ---------------------------------------------------------------------------
# Persistent config (api_key + instance_id survive restarts)
# Each cwd gets its own slot in the config file so multiple instances on the
# same machine don't collide.
# ---------------------------------------------------------------------------

def _load_cfg() -> dict:
    if CONFIG_FILE.exists():
        try:
            all_cfg = json.loads(CONFIG_FILE.read_text())
            return all_cfg.get(_CWD_KEY, {})
        except Exception:
            pass
    return {}


def _save_cfg(data: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        all_cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        all_cfg = {}
    all_cfg[_CWD_KEY] = data
    CONFIG_FILE.write_text(json.dumps(all_cfg, indent=2))


# ---------------------------------------------------------------------------
# Runtime state (reset on each process start)
# ---------------------------------------------------------------------------

_cfg: dict = {}
_http: Optional[httpx.AsyncClient] = None
_inbox: list[dict] = []       # push messages buffered from WebSocket
_connected: bool = False


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(base_url=SERVER_URL, timeout=10.0)
    return _http


def _headers() -> dict:
    h: dict = {}
    api_key = _cfg.get("api_key", "")
    if api_key:
        h["X-API-Key"] = api_key
    iid = _cfg.get("instance_id", "")
    if iid:
        h["X-Instance-Id"] = iid
    return h


def _system_info() -> dict:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "platform_version": platform.version(),
        "python": platform.python_version(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
    }


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _heartbeat_loop():
    http = _get_http()
    while True:
        await asyncio.sleep(15)
        try:
            await http.post(
                "/api/heartbeat",
                headers=_headers(),
                json={"hook_mode": _cfg.get("hook_mode", "off")},
            )
        except Exception:
            pass


def _write_incoming(msg: dict):
    """Append a message to incoming.json for Claude Code hook scripts to read."""
    try:
        INCOMING_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(INCOMING_FILE.read_text()) if INCOMING_FILE.exists() else []
        if not isinstance(existing, list):
            existing = []
        existing.append(msg)
        INCOMING_FILE.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


async def _ws_listener():
    """Maintain a persistent WebSocket to receive push messages."""
    api_key = _cfg.get("api_key", "")
    iid     = _cfg.get("instance_id", "")
    ws_base = SERVER_URL.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_base}/ws/instance?api_key={api_key}&instance_id={iid}"
    while True:
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                    elif msg.get("type") == "set_hook_mode":
                        mode = msg.get("mode")
                        if mode in HOOK_MODES:
                            _cfg["hook_mode"] = mode
                            _save_cfg(_cfg)
                    elif msg.get("type") == "message":
                        _inbox.append(msg)
                        # Cap buffer at 200 messages
                        if len(_inbox) > 200:
                            _inbox.pop(0)
                        # Write to incoming.json for hook scripts
                        _write_incoming(msg)
        except Exception:
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def connect(name: str = "", instance_type: str = "claude-code") -> str:
    """
    Register this instance with the AI Mesh server.
    Call this first before using any other tool.
    Requires an API key — call set_api_key() first if you don't have one yet.

    Args:
        name: Display name for this instance. Defaults to hostname-pid.
        instance_type: Type label, e.g. 'claude-code', 'claude-api', 'worker'.
    """
    global _cfg, _connected

    _cfg = _load_cfg()
    http = _get_http()

    # API key is required
    if not _cfg.get("api_key"):
        return (
            "No API key configured.\n"
            f"1. Open the web GUI at {SERVER_URL}\n"
            "2. Log in and go to your account → API Keys → Generate Key\n"
            "3. Copy the key (shown only once) and run: set_api_key('mesh_...')"
        )

    # Try to reconnect if already registered
    if _cfg.get("instance_id"):
        try:
            r = await http.post("/api/heartbeat", headers=_headers())
            if r.status_code == 200:
                _connected = True
                asyncio.create_task(_heartbeat_loop())
                asyncio.create_task(_ws_listener())
                iid = _cfg["instance_id"]
                stored_name = _cfg.get("name", iid)
                return (
                    f"Reconnected to AI Mesh as '{stored_name}' "
                    f"(ID: {iid}) at {SERVER_URL}"
                )
        except Exception:
            pass

    # Fresh registration
    if not name:
        name = f"{socket.gethostname()}-{os.getpid()}"

    r = await http.post(
        "/api/register",
        headers=_headers(),
        json={
            "name": name,
            "instance_type": instance_type,
            "system_info": _system_info(),
        },
    )
    r.raise_for_status()
    data = r.json()

    _cfg.update({
        "instance_id": data["instance_id"],
        "name": name,
        "server_url": SERVER_URL,
    })
    _save_cfg(_cfg)
    _connected = True

    asyncio.create_task(_heartbeat_loop())
    asyncio.create_task(_ws_listener())

    return (
        f"Connected to AI Mesh as '{name}' "
        f"(ID: {data['instance_id']}) at {SERVER_URL}\n"
        f"Config saved to {CONFIG_FILE}"
    )


@mcp.tool()
async def send_message(content: str, to_instance_id: str = "") -> str:
    """
    Send a message to another instance or broadcast to all.

    Args:
        content: The message text.
        to_instance_id: Target instance ID. Leave empty to broadcast to everyone.
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    http = _get_http()
    payload = {"content": content, "to_id": to_instance_id or None}
    r = await http.post("/api/messages", headers=_headers(), json=payload)
    r.raise_for_status()

    target = f"instance {to_instance_id}" if to_instance_id else "all instances (broadcast)"
    return f"Message sent to {target}."


@mcp.tool()
async def check_inbox() -> str:
    """
    Return all push messages that arrived since the last call, then clear the buffer.
    This is the fastest way to see if another instance has messaged you.
    """
    if not _inbox:
        return "Inbox is empty."

    msgs = list(_inbox)
    _inbox.clear()

    lines = []
    for m in msgs:
        ts = time.strftime("%H:%M:%S", time.localtime(m.get("timestamp", 0)))
        sender = m.get("from_name") or m.get("from_id", "?")
        to = m.get("to_id")
        target = f"→ {to}" if to else "(broadcast)"
        lines.append(f"[{ts}] {sender} {target}: {m.get('content','')}")

    return "\n".join(lines)


@mcp.tool()
async def get_messages(limit: int = 20) -> str:
    """
    Fetch recent message history from the server for this instance (includes broadcasts).

    Args:
        limit: Max number of messages to return (newest first).
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    http = _get_http()
    r = await http.get(f"/api/messages?limit={limit}", headers=_headers())
    r.raise_for_status()
    msgs = r.json()

    if not msgs:
        return "No messages found."

    lines = []
    for m in msgs:
        ts = time.strftime("%H:%M:%S", time.localtime(m.get("timestamp", 0)))
        sender = m.get("from_display") or m.get("from_name") or m.get("from_id", "?")
        to = m.get("to_id")
        target = f"→ {to}" if to else "(broadcast)"
        lines.append(f"[{ts}] {sender} {target}: {m.get('content','')}")

    return "\n".join(lines)


@mcp.tool()
async def list_instances() -> str:
    """
    List all instances registered with the mesh server (online and offline).
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    http = _get_http()
    r = await http.get("/api/instances", headers=_headers())
    r.raise_for_status()
    instances = r.json()

    if not instances:
        return "No instances registered."

    my_id = _cfg.get("instance_id", "")
    lines = []
    for i in instances:
        if i["id"] == "admin":
            continue
        status = "🟢" if i["connected"] else "⚫"
        name = i.get("display_name") or i["name"]
        sys_info = json.loads(i.get("system_info") or "{}")
        host = sys_info.get("hostname", "")
        me = " (you)" if i["id"] == my_id else ""
        lines.append(
            f"{status} {name}{me}  ID:{i['id']}  [{i['instance_type']}]  {host}"
        )

    return "\n".join(lines)


@mcp.tool()
async def set_name(name: str) -> str:
    """
    Update this instance's display name on the mesh server.

    Args:
        name: New name for this instance.
    """
    if not _cfg.get("api_key"):
        return "Not connected. Call connect() first."

    iid = _cfg["instance_id"]
    http = _get_http()
    r = await http.patch(
        f"/api/instances/{iid}",
        headers=_headers(),
        json={"name": name},
    )
    r.raise_for_status()
    _cfg["name"] = name
    _save_cfg(_cfg)
    return f"Name updated to '{name}'."


@mcp.tool()
async def my_info() -> str:
    """
    Show this instance's registration details (ID, name, server URL).
    """
    if not _cfg:
        return "Not connected. Call connect() first."
    api_key = _cfg.get("api_key", "")
    key_display = (api_key[:13] + "...") if api_key else "(none)"
    return json.dumps(
        {
            "instance_id": _cfg.get("instance_id"),
            "name": _cfg.get("name"),
            "server_url": _cfg.get("server_url", SERVER_URL),
            "api_key": key_display,
            "config_file": str(CONFIG_FILE),
            "connected": _connected,
        },
        indent=2,
    )


@mcp.tool()
async def set_api_key(key: str) -> str:
    """
    Store an API key for authenticating with the AI Mesh server.
    Generate a key from the web GUI: Log in → API Keys → Generate Key.
    The key is stored in config and used for all subsequent requests.

    Args:
        key: API key starting with 'mesh_'.
    """
    global _cfg
    if not key.startswith("mesh_"):
        return "Invalid key format. Keys must start with 'mesh_'."
    _cfg = _load_cfg()
    _cfg["api_key"] = key
    # Clear stale instance_id so connect() re-registers with the new key
    _cfg.pop("instance_id", None)
    _save_cfg(_cfg)
    prefix = key[:13] + "..."
    return (
        f"API key saved ({prefix}). Call connect() to register this instance."
    )


HOOK_MODES = ("off", "prompt", "tool", "both")


@mcp.tool()
async def set_hook_mode(mode: str) -> str:
    """
    Set the Claude Code hook injection mode for incoming AI Mesh messages.

    Modes:
      off    - hooks disabled, no injection (default)
      prompt - inject pending messages at the top of your next prompt (UserPromptSubmit)
      tool   - inject pending messages after each tool use (PostToolUse)
      both   - inject on both events

    Args:
        mode: One of 'off', 'prompt', 'tool', 'both'.
    """
    if mode not in HOOK_MODES:
        return f"Invalid mode '{mode}'. Choose from: {', '.join(HOOK_MODES)}"
    _cfg["hook_mode"] = mode
    _save_cfg(_cfg)
    if mode == "off":
        return "Hook injection disabled. Messages still buffered in check_inbox()."
    return f"Hook mode set to '{mode}'. Incoming messages will be injected into your Claude Code session."


@mcp.tool()
async def get_hook_mode() -> str:
    """
    Get the current Claude Code hook injection mode.
    """
    mode = _cfg.get("hook_mode", "off")
    descriptions = {
        "off":    "disabled — no automatic injection",
        "prompt": "inject on UserPromptSubmit (prepended to your next prompt)",
        "tool":   "inject on PostToolUse (after each tool call during active tasks)",
        "both":   "inject on both UserPromptSubmit and PostToolUse",
    }
    return f"Current hook mode: '{mode}' — {descriptions.get(mode, '')}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
