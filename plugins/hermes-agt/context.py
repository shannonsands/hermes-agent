"""Translate Hermes tool calls to AGT policy-evaluation context dicts.

The AGT PolicyEvaluator takes a flat dict of fields and matches them against
PolicyRule conditions (field + operator + value). We map each Hermes tool to
a canonical context shape so that policies are portable across tools — e.g.
a "block writes outside ~/projects" policy works the same whether Hermes
calls `write_file`, `patch`, or a shell `tee` command.

Canonical fields emitted for every tool:

    tool_name           Hermes tool name (e.g. "terminal", "write_file")
    tool_kind           Coarse category: shell | file_read | file_write |
                        code_exec | network | mcp | meta | other
    agent_did           Stable identifier for the calling Hermes session
    session_id          Hermes session id (may differ from agent_did)
    platform            "cli" | "telegram" | "discord" | ...
    yolo_mode           True if the user is running with --yolo / approvals.mode=off

Tool-specific fields are added on top — keep them flat (no nesting) so the
PolicyEvaluator's operators (EQ, IN, CONTAINS, MATCHES, ...) can match on
them directly.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# Tools that read state without mutating it. Policies usually allow these
# freely.
_READ_TOOLS = {
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "vision_analyze", "memory",  # memory is read+write but low-risk
    "todo",
}

# Tools that mutate the local filesystem.
_FILE_WRITE_TOOLS = {"write_file", "patch"}

# Tools that exec code or shell commands.
_CODE_EXEC_TOOLS = {"terminal", "execute_code", "process"}

# Tools that reach the network.
_NETWORK_TOOLS = {"web_search", "browser", "fetch", "download"}

# Agent-loop / meta tools (delegation, scheduling, skill management).
_META_TOOLS = {
    "delegate_task", "cronjob", "skill_manage", "clarify",
    "text_to_speech", "image_gen",
}


def _classify_tool(tool_name: str) -> str:
    if tool_name in _READ_TOOLS:
        # split read-only files vs other reads so file policies are sharper
        if tool_name in {"read_file", "search_files"}:
            return "file_read"
        return "other"
    if tool_name in _FILE_WRITE_TOOLS:
        return "file_write"
    if tool_name in _CODE_EXEC_TOOLS:
        return "code_exec"
    if tool_name in _NETWORK_TOOLS:
        return "network"
    if tool_name in _META_TOOLS:
        return "meta"
    if tool_name.startswith("mcp__") or tool_name.startswith("mcp_"):
        return "mcp"
    return "other"


def _safe(d: Dict[str, Any], key: str, default: Any = "") -> Any:
    """Get a key from a possibly-non-dict structure without raising."""
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def build_context(
    tool_name: str,
    args: Dict[str, Any],
    *,
    session_id: str = "",
    task_id: str = "",
    platform: str = "",
    agent_did: str = "",
) -> Dict[str, Any]:
    """Build the AGT policy-evaluation context for a Hermes tool call.

    Returns a flat dict suitable for ``PolicyEvaluator.evaluate(ctx)``.
    Tool-specific fields are included on top of the common base so policy
    authors can write rules like::

        condition: { field: "command", operator: matches, value: "rm -rf .*" }
    """
    args = args if isinstance(args, dict) else {}

    ctx: Dict[str, Any] = {
        "tool_name": tool_name,
        "tool_kind": _classify_tool(tool_name),
        "agent_did": agent_did or f"did:hermes:{session_id or 'local'}",
        "session_id": session_id,
        "task_id": task_id,
        "platform": platform or os.environ.get("HERMES_SESSION_PLATFORM", "cli"),
        "yolo_mode": _is_yolo(),
    }

    # Tool-specific projection. Keep field names canonical and stable —
    # policies in the wild will reference them.
    if tool_name == "terminal":
        ctx["command"] = _safe(args, "command")
        ctx["background"] = bool(_safe(args, "background", False))
        ctx["pty"] = bool(_safe(args, "pty", False))
        ctx["workdir"] = _safe(args, "workdir")
    elif tool_name in ("write_file", "patch"):
        ctx["path"] = _safe(args, "path")
        # patch carries old_string/new_string; expose lengths only — the
        # actual content is too noisy for policy matching, but length-based
        # rules ("block patches over 10KB") are useful.
        if tool_name == "patch":
            ctx["mode"] = _safe(args, "mode", "replace")
            ctx["old_len"] = len(_safe(args, "old_string", "") or "")
            ctx["new_len"] = len(_safe(args, "new_string", "") or "")
        else:
            ctx["content_len"] = len(_safe(args, "content", "") or "")
    elif tool_name == "read_file":
        ctx["path"] = _safe(args, "path")
        ctx["limit"] = _safe(args, "limit", 500)
    elif tool_name == "search_files":
        ctx["pattern"] = _safe(args, "pattern")
        ctx["path"] = _safe(args, "path", ".")
        ctx["target"] = _safe(args, "target", "content")
    elif tool_name == "execute_code":
        code = _safe(args, "code", "")
        ctx["code_len"] = len(code or "")
        # First non-comment line is a useful heuristic for policy matching
        # without dragging the whole script into the audit log.
        for line in (code or "").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                ctx["code_first_line"] = stripped[:200]
                break
    elif tool_name == "process":
        ctx["action"] = _safe(args, "action")
        ctx["session_id_target"] = _safe(args, "session_id")
    elif tool_name == "delegate_task":
        ctx["goal"] = (_safe(args, "goal") or "")[:200]
        ctx["toolsets"] = ",".join(_safe(args, "toolsets", []) or [])
        ctx["batch"] = bool(_safe(args, "tasks"))
    elif tool_name == "cronjob":
        ctx["action"] = _safe(args, "action")
        ctx["schedule"] = _safe(args, "schedule")

    return ctx


def _is_yolo() -> bool:
    val = (os.environ.get("HERMES_YOLO_MODE") or "").strip().lower()
    return val in {"1", "true", "yes", "on"}
