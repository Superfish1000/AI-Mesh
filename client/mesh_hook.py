"""
AI Mesh Claude Code Hook Script
Handles both UserPromptSubmit and PostToolUse hooks.
Reads pending mesh messages from ~/.ai-mesh/incoming.json and injects them
into the active Claude Code session based on the configured hook_mode.

Hook mode is set per-cwd in ~/.ai-mesh/config.json:
  "off"    - do nothing (default)
  "prompt" - inject on next UserPromptSubmit
  "tool"   - inject on every PostToolUse
  "both"   - inject on both

Claude Code registers this script in ~/.claude/settings.json hooks.
Called by Claude Code with the hook type as the first argument:
  python mesh_hook.py UserPromptSubmit
  python mesh_hook.py PostToolUse

Input:  JSON on stdin (Claude Code hook payload)
Output: JSON on stdout to modify Claude's context, or empty to pass through
"""

import json
import sys
import time
from pathlib import Path

CONFIG_FILE = Path.home() / ".ai-mesh" / "config.json"
INCOMING_FILE = Path.home() / ".ai-mesh" / "incoming.json"
CWD_KEY = str(Path.cwd())

HOOK_TYPE = sys.argv[1] if len(sys.argv) > 1 else "UserPromptSubmit"


def load_cfg() -> dict:
    if CONFIG_FILE.exists():
        try:
            all_cfg = json.loads(CONFIG_FILE.read_text())
            return all_cfg.get(CWD_KEY, {})
        except Exception:
            pass
    return {}


def read_and_clear_inbox() -> list[dict]:
    """Read pending messages and clear the file atomically."""
    if not INCOMING_FILE.exists():
        return []
    try:
        msgs = json.loads(INCOMING_FILE.read_text())
        INCOMING_FILE.write_text("[]")
        return msgs if isinstance(msgs, list) else []
    except Exception:
        return []


def format_messages(msgs: list[dict]) -> str:
    lines = ["⚡ AI Mesh — incoming message(s):"]
    for m in msgs:
        ts = time.strftime("%H:%M:%S", time.localtime(m.get("timestamp", 0)))
        sender = m.get("from_name") or m.get("from_id", "?")
        to = m.get("to_id")
        target = f"→ you" if to else "(broadcast)"
        lines.append(f"  [{ts}] {sender} {target}: {m.get('content', '')}")
    return "\n".join(lines)


def main():
    # Read stdin (Claude Code hook payload)
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        payload = {}

    cfg = load_cfg()
    hook_mode = cfg.get("hook_mode", "off")

    # Determine if this hook type is active
    should_fire = (
        hook_mode == "both"
        or (hook_mode == "prompt" and HOOK_TYPE == "UserPromptSubmit")
        or (hook_mode == "tool"   and HOOK_TYPE == "PostToolUse")
    )

    if not should_fire:
        # Pass through unchanged
        print(json.dumps(payload))
        return

    msgs = read_and_clear_inbox()
    if not msgs:
        print(json.dumps(payload))
        return

    injection = format_messages(msgs)

    if HOOK_TYPE == "UserPromptSubmit":
        # Prepend mesh messages to the user's prompt
        original_prompt = payload.get("prompt", "")
        payload["prompt"] = f"{injection}\n\n{original_prompt}" if original_prompt else injection
        print(json.dumps(payload))

    elif HOOK_TYPE == "PostToolUse":
        # Output an additional system message after the tool result
        # Claude Code PostToolUse hooks can emit a "system" injection
        output = {**payload, "system_injection": injection}
        print(json.dumps(output))


if __name__ == "__main__":
    main()
