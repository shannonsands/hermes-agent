#!/usr/bin/env python3
"""Vulcan bridge proxy tools for Hermes.

These tools remain normal Hermes tools from the model's perspective, but
forward execution across the Vulcan↔Hermes JSON-lines bridge so Hermes can use
selected Vulcan-native capabilities without surrendering control of its own
agent loop.
"""

import json
import os
import threading
import uuid
from typing import Any, Dict, Optional

from tools.registry import registry

try:
    import __main__ as _bridge_main
except Exception:  # pragma: no cover
    _bridge_main = None


_CALL_TIMEOUT_SECONDS = 300
_BRIDGE_PROFILE = os.getenv("VULCAN_HERMES_BRIDGE_PROFILE", "").strip().lower()


def _call_vulcan_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if _bridge_main is None:
        return {"ok": False, "error": "Vulcan bridge runtime is unavailable"}

    emit = getattr(_bridge_main, "emit", None)
    pending = getattr(_bridge_main, "_pending_vulcan_results", None)
    lock = getattr(_bridge_main, "_pending_vulcan_lock", None)

    if not callable(emit) or pending is None or lock is None:
        return {"ok": False, "error": "Vulcan bridge RPC hooks are unavailable"}

    request_id = f"vt_{uuid.uuid4().hex[:12]}"
    event = threading.Event()

    with lock:
        pending[request_id] = {"event": event, "response": None}

    emit({
        "type": "vulcan_tool_call",
        "id": request_id,
        "tool": tool_name,
        "args": args or {},
    })

    if not event.wait(timeout=_CALL_TIMEOUT_SECONDS):
        with lock:
            pending.pop(request_id, None)
        return {"ok": False, "error": f"Timed out waiting for Vulcan tool result: {tool_name}"}

    with lock:
        payload = pending.pop(request_id, None)

    return (payload or {}).get("response") or {
        "ok": False,
        "error": f"Missing Vulcan tool result: {tool_name}",
    }


def _normalize_result(response: Dict[str, Any]) -> str:
    if not isinstance(response, dict):
        return json.dumps({"error": f"Invalid Vulcan bridge response: {response!r}"}, ensure_ascii=False)

    if not response.get("ok"):
        message = response.get("error") or "Unknown Vulcan tool error"
        details = response.get("details") or {}
        return json.dumps({"error": message, "details": details}, ensure_ascii=False)

    payload = response.get("result")
    if not isinstance(payload, dict):
        payload = {
            "content": str(payload or ""),
            "details": {},
            "is_error": False,
        }

    return json.dumps(payload, ensure_ascii=False)


def _proxy(tool_name: str, args: Optional[Dict[str, Any]]) -> str:
    return _normalize_result(_call_vulcan_tool(tool_name, args or {}))


def _schema_alias(schema: Dict[str, Any], name: str, description: Optional[str] = None) -> Dict[str, Any]:
    cloned = dict(schema)
    cloned["name"] = name
    if description is not None:
        cloned["description"] = description
    return cloned


def _register_alias(name: str, target_tool_name: str, schema: Dict[str, Any], toolset: str) -> None:
    registry.register(
        name=name,
        toolset=toolset,
        schema=_schema_alias(schema, name),
        handler=lambda args, _target=target_tool_name, **kw: _proxy(_target, args),
        check_fn=check_vulcan_requirements,
        emoji="🌋",
    )


def check_vulcan_requirements() -> bool:
    return True


VULCAN_READ_SCHEMA = {
    "name": "vulcan_read",
    "description": "Read a file using Vulcan's host/sandbox-aware read tool.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
            "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
            "limit": {"type": "integer", "description": "Maximum lines to read"}
        },
        "required": ["path"]
    }
}

VULCAN_WRITE_SCHEMA = {
    "name": "vulcan_write",
    "description": "Write content to a file using Vulcan's write tool.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write"},
            "content": {"type": "string", "description": "Content to write"}
        },
        "required": ["path", "content"]
    }
}

VULCAN_EDIT_SCHEMA = {
    "name": "vulcan_edit",
    "description": "Edit a file using Vulcan's exact-match edit tool.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "oldText": {"type": "string", "description": "Exact text to replace"},
            "newText": {"type": "string", "description": "Replacement text"}
        },
        "required": ["path", "oldText", "newText"]
    }
}

VULCAN_APPLY_PATCH_SCHEMA = {
    "name": "vulcan_apply_patch",
    "description": "Apply a structured multi-file patch using Vulcan's apply_patch tool.",
    "parameters": {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "Full patch text beginning with *** Begin Patch and ending with *** End Patch"
            }
        },
        "required": ["patch"]
    }
}

VULCAN_BASH_SCHEMA = {
    "name": "vulcan_bash",
    "description": "Execute a bash command through Vulcan's bash tool.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Bash command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds"}
        },
        "required": ["command"]
    }
}

VULCAN_TODO_SCHEMA = {
    "name": "vulcan_todo",
    "description": "Read or update Vulcan's session todo list.",
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"]
                        }
                    },
                    "required": ["id", "content", "status"]
                }
            },
            "merge": {"type": "boolean", "description": "Merge instead of replace."}
        },
        "required": []
    }
}

VULCAN_IEX_SCHEMA = {
    "name": "vulcan_iex",
    "description": (
        "Execute Elixir code directly in Vulcan's live host BEAM VM. "
        "This can inspect or modify runtime state and call internal modules directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Elixir code to evaluate"},
            "timeout": {"type": "integer", "description": "Timeout in seconds"}
        },
        "required": ["code"]
    }
}

VULCAN_MEMORY_SCHEMA = {
    "name": "vulcan_memory",
    "description": "Access Vulcan's long-term memory graph for search, store, recall, link, and diagnostics.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "store", "search", "recall", "link", "merge", "stats",
                    "find_duplicates", "find_orphans", "find_clusters", "link_orphans",
                    "score", "prune", "explore"
                ]
            },
            "content": {"type": "string"},
            "type": {"type": "string", "enum": ["fact", "entity", "preference", "decision", "reflection"]},
            "scope": {"type": "string"},
            "from_id": {"type": "string"},
            "to_id": {"type": "string"},
            "relation": {"type": "string"},
            "keep_id": {"type": "string"},
            "remove_ids": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["action"]
    }
}

VULCAN_PROJECT_SKILL_SCHEMA = {
    "name": "vulcan_project_skill",
    "description": "Manage project-local Vulcan/Open Manus skills and runbooks.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["paths", "list", "read", "record", "delete"]},
            "skill": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["replace", "append"]}
        },
        "required": ["action"]
    }
}

VULCAN_SHARED_SKILL_SCHEMA = {
    "name": "vulcan_shared_skill",
    "description": "Manage shared Vulcan/Open Manus skills and reusable runbooks.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["paths", "list", "read", "record", "delete", "promote_from_project"]},
            "skill": {"type": "string"},
            "source_skill": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["replace", "append"]}
        },
        "required": ["action"]
    }
}

VULCAN_INTROSPECT_SCHEMA = {
    "name": "vulcan_introspect",
    "description": "Inspect Vulcan capabilities by category.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["tools", "skills", "hooks", "extensions", "automations", "subagents", "templates", "docs", "cluster", "memory"]
            }
        },
        "required": ["category"]
    }
}

VULCAN_SANDBOXED_IEX_SCHEMA = {
    "name": "vulcan_sandboxed_iex",
    "description": "Execute Elixir code on Vulcan's sandbox node through sandboxed_iex.",
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Elixir code to evaluate on the sandbox node"},
            "timeout": {"type": "integer", "description": "Timeout in seconds"}
        },
        "required": ["code"]
    }
}

VULCAN_TMUX_SCHEMA = {
    "name": "vulcan_tmux",
    "description": "Manage persistent tmux sessions inside Vulcan's sandbox-aware execution environment.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["new", "send", "capture", "list", "kill", "wait"],
                "description": "Tmux action."
            },
            "session": {"type": "string", "description": "Session name"},
            "command": {"type": "string", "description": "Command or keys, depending on action"},
            "delay": {"type": "integer", "description": "Delay in ms for wait"},
            "enter": {"type": "boolean", "description": "Append Enter after send"},
            "lines": {"type": "integer", "description": "Capture line count"}
        },
        "required": ["action"]
    }
}

VULCAN_BROWSER_SCHEMA = {
    "name": "vulcan_browser",
    "description": "Control Vulcan's persistent browser session, including the Open Manus Display-tab browser.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "screenshot", "fetch", "evaluate", "navigate", "wait_for", "snapshot",
                    "click", "hover", "drag", "fill", "press_key", "upload_file",
                    "list_pages", "new_page", "select_page", "close_page", "resize_page",
                    "console", "network", "handle_dialog", "emulate", "performance"
                ],
                "description": "Browser action."
            },
            "url": {"type": "string"},
            "type": {"type": "string"},
            "selector": {"type": "string"},
            "uid": {"type": "string"},
            "source_selector": {"type": "string"},
            "source_uid": {"type": "string"},
            "target_selector": {"type": "string"},
            "target_uid": {"type": "string"},
            "text": {"type": "string"},
            "value": {"type": "string"},
            "javascript": {"type": "string"},
            "key": {"type": "string"},
            "keys": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "full_page": {"type": "boolean"},
            "timeout": {"type": "integer"},
            "limit": {"type": "integer"},
            "dbl_click": {"type": "boolean"},
            "page_id": {"type": "string"},
            "index": {"type": "integer"},
            "select": {"type": "boolean"},
            "ignore_cache": {"type": "boolean"},
            "mode": {"type": "string"},
            "entry_id": {"type": "integer"},
            "request_id": {"type": "string"},
            "file_path": {"type": "string"},
            "file_paths": {"type": "array", "items": {"type": "string"}},
            "mobile": {"type": "boolean"},
            "device_scale_factor": {"type": "number"},
            "user_agent": {"type": "string"},
            "color_scheme": {"type": "string"},
            "prompt_text": {"type": "string"}
        },
        "required": ["action"]
    }
}

VULCAN_GUI_SCHEMA = {
    "name": "vulcan_gui",
    "description": "Control Vulcan's sandbox desktop for GUI interactions in the Open Manus Display surface.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "screenshot", "list_windows", "active_window", "focus_window",
                    "click", "double_click", "move_mouse", "drag", "scroll",
                    "type", "key", "wait_for_window"
                ],
                "description": "GUI action."
            },
            "path": {"type": "string"},
            "target": {"type": "string"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "to_x": {"type": "integer"},
            "to_y": {"type": "integer"},
            "button": {"type": "string"},
            "amount": {"type": "integer"},
            "text": {"type": "string"},
            "keys": {"type": "string"},
            "window_id": {"type": "string"},
            "window_name": {"type": "string"},
            "timeout": {"type": "integer"},
            "delay_ms": {"type": "integer"}
        },
        "required": ["action"]
    }
}

VULCAN_WORKSPACE_SCHEMA = {
    "name": "vulcan_workspace",
    "description": "Manage the main Vulcan chat workspace execution mode, sandbox lifecycle, and active workspace.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["toggle", "status", "set_mode", "restart_container", "set_workspace"],
                "description": "Workspace action."
            },
            "mode": {"type": "string", "enum": ["host", "sandbox"]},
            "workspace": {"type": "string"}
        },
        "required": ["action"]
    }
}

VULCAN_WEB_SCHEMA = {
    "name": "vulcan_web",
    "description": "Search or crawl the web through Vulcan's web research tool.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "crawl", "extract", "deep_crawl"],
                "description": "Web action."
            },
            "query": {"type": "string"},
            "url": {"type": "string"},
            "engine": {"type": "string", "enum": ["auto", "http", "browser"]},
            "provider": {"type": "string"},
            "max_results": {"type": "integer"},
            "ttl_seconds": {"type": "integer"},
            "max_chars": {"type": "integer"},
            "selector": {"type": "string"},
            "max_chunks": {"type": "integer"},
            "max_depth": {"type": "integer"},
            "max_pages": {"type": "integer"},
            "max_concurrency": {"type": "integer"},
            "task_timeout": {"type": "integer"},
            "retry_attempts": {"type": "integer"},
            "retry_base_delay_ms": {"type": "integer"},
            "retry_max_delay_ms": {"type": "integer"},
            "include_external": {"type": "boolean"},
            "use_seeds": {"type": "boolean"},
            "seed_limit": {"type": "integer"},
            "include_common_paths": {"type": "boolean"},
            "pattern": {"type": "string"}
        },
        "required": ["action"]
    }
}

VULCAN_WORKSPACE_UI_SCHEMA = {
    "name": "vulcan_workspace_ui",
    "description": "Control the built-in Vulcan chat workspace panel, tabs, preview, and workspace modal.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "workspace", "tab", "preview", "modal"],
                "description": "Built-in workspace UI action."
            },
            "chat_id": {"type": "string"},
            "panel_mode": {"type": "string", "enum": ["hidden", "collapsed", "split", "focused"]},
            "width_ratio": {"type": "number"},
            "tab": {
                "type": "string",
                "enum": ["preview", "files", "terminal", "display", "jobs", "artifacts", "plan"]
            },
            "viewport": {"type": "string", "enum": ["desktop", "mobile"]},
            "preview_action": {"type": "string", "enum": ["show", "clear", "auto"]},
            "target_kind": {"type": "string", "enum": ["artifact", "guest_path", "url"]},
            "target": {"type": "string"},
            "preview_type": {"type": "string", "enum": ["web", "image", "code", "document", "text", "data"]},
            "source": {"type": "string", "enum": ["agent", "files", "artifacts", "system"]},
            "label": {"type": "string"},
            "pinned": {"type": "boolean"},
            "modal": {"type": "string", "enum": ["workspace_controls"]}
        },
        "required": ["action"]
    }
}

VULCAN_MANUS_UI_SCHEMA = {
    "name": "vulcan_manus_ui",
    "description": "Control the Open Manus workspace panel, tabs, preview, and managed package-runtime launch path.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "workspace", "tab", "preview", "launch_project_app"],
                "description": "Open Manus UI action."
            },
            "chat_id": {"type": "string"},
            "panel_mode": {"type": "string", "enum": ["collapsed", "split", "focused"]},
            "width_ratio": {"type": "number"},
            "tab": {"type": "string", "enum": ["terminal", "files", "preview", "display"]},
            "viewport": {"type": "string", "enum": ["desktop", "mobile"]},
            "preview_action": {"type": "string", "enum": ["show", "clear", "auto"]},
            "target_kind": {"type": "string", "enum": ["artifact", "guest_path", "url"]},
            "target": {"type": "string"},
            "preview_type": {"type": "string", "enum": ["web", "image", "code", "document", "text", "data"]},
            "source": {"type": "string", "enum": ["agent", "files", "artifacts", "system"]},
            "label": {"type": "string"},
            "pinned": {"type": "boolean"},
            "launch_command": {"type": "string"},
            "launch_cwd": {"type": "string"},
            "launch_background": {"type": "boolean"}
        },
        "required": ["action"]
    }
}

VULCAN_RESET_CONTAINER_SCHEMA = {
    "name": "vulcan_reset_container",
    "description": "Reset the current Open Manus sandbox container and reconnect to a fresh one.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Optional reason for the reset."}
        },
        "required": []
    }
}


registry.register(
    name="vulcan_read",
    toolset="vulcan",
    schema=VULCAN_READ_SCHEMA,
    handler=lambda args, **kw: _proxy("read", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_write",
    toolset="vulcan",
    schema=VULCAN_WRITE_SCHEMA,
    handler=lambda args, **kw: _proxy("write", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_edit",
    toolset="vulcan",
    schema=VULCAN_EDIT_SCHEMA,
    handler=lambda args, **kw: _proxy("edit", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_apply_patch",
    toolset="vulcan",
    schema=VULCAN_APPLY_PATCH_SCHEMA,
    handler=lambda args, **kw: _proxy("apply_patch", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_bash",
    toolset="vulcan",
    schema=VULCAN_BASH_SCHEMA,
    handler=lambda args, **kw: _proxy("bash", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_todo",
    toolset="vulcan",
    schema=VULCAN_TODO_SCHEMA,
    handler=lambda args, **kw: _proxy("todo", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_iex",
    toolset="vulcan",
    schema=VULCAN_IEX_SCHEMA,
    handler=lambda args, **kw: _proxy("iex", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_memory",
    toolset="vulcan",
    schema=VULCAN_MEMORY_SCHEMA,
    handler=lambda args, **kw: _proxy("memory", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_project_skill",
    toolset="vulcan",
    schema=VULCAN_PROJECT_SKILL_SCHEMA,
    handler=lambda args, **kw: _proxy("project_skill", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_shared_skill",
    toolset="vulcan",
    schema=VULCAN_SHARED_SKILL_SCHEMA,
    handler=lambda args, **kw: _proxy("shared_skill", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_introspect",
    toolset="vulcan",
    schema=VULCAN_INTROSPECT_SCHEMA,
    handler=lambda args, **kw: _proxy("introspect", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_sandboxed_iex",
    toolset="vulcan",
    schema=VULCAN_SANDBOXED_IEX_SCHEMA,
    handler=lambda args, **kw: _proxy("sandboxed_iex", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_tmux",
    toolset="vulcan",
    schema=VULCAN_TMUX_SCHEMA,
    handler=lambda args, **kw: _proxy("tmux", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_browser",
    toolset="vulcan",
    schema=VULCAN_BROWSER_SCHEMA,
    handler=lambda args, **kw: _proxy("browser", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_gui",
    toolset="vulcan",
    schema=VULCAN_GUI_SCHEMA,
    handler=lambda args, **kw: _proxy("gui", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_web",
    toolset="vulcan",
    schema=VULCAN_WEB_SCHEMA,
    handler=lambda args, **kw: _proxy("web", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_workspace",
    toolset="vulcan",
    schema=VULCAN_WORKSPACE_SCHEMA,
    handler=lambda args, **kw: _proxy("workspace", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_workspace_ui",
    toolset="vulcan",
    schema=VULCAN_WORKSPACE_UI_SCHEMA,
    handler=lambda args, **kw: _proxy("workspace_ui", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_manus_ui",
    toolset="vulcan",
    schema=VULCAN_MANUS_UI_SCHEMA,
    handler=lambda args, **kw: _proxy("manus_ui", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)

registry.register(
    name="vulcan_reset_container",
    toolset="vulcan",
    schema=VULCAN_RESET_CONTAINER_SCHEMA,
    handler=lambda args, **kw: _proxy("reset_container", args),
    check_fn=check_vulcan_requirements,
    emoji="🌋",
)


if _BRIDGE_PROFILE == "main_ui":
    _register_alias("read", "read", VULCAN_READ_SCHEMA, "main_ui")
    _register_alias("write", "write", VULCAN_WRITE_SCHEMA, "main_ui")
    _register_alias("edit", "edit", VULCAN_EDIT_SCHEMA, "main_ui")
    _register_alias("apply_patch", "apply_patch", VULCAN_APPLY_PATCH_SCHEMA, "main_ui")
    _register_alias("bash", "bash", VULCAN_BASH_SCHEMA, "main_ui")
    _register_alias("todo", "todo", VULCAN_TODO_SCHEMA, "main_ui")
    _register_alias("iex", "iex", VULCAN_IEX_SCHEMA, "main_ui")
    _register_alias("memory", "memory", VULCAN_MEMORY_SCHEMA, "main_ui")
    _register_alias(
        "project_skill",
        "project_skill",
        VULCAN_PROJECT_SKILL_SCHEMA,
        "main_ui",
    )
    _register_alias(
        "shared_skill",
        "shared_skill",
        VULCAN_SHARED_SKILL_SCHEMA,
        "main_ui",
    )
    _register_alias("introspect", "introspect", VULCAN_INTROSPECT_SCHEMA, "main_ui")
    _register_alias("sandboxed_iex", "sandboxed_iex", VULCAN_SANDBOXED_IEX_SCHEMA, "main_ui")
    _register_alias("tmux", "tmux", VULCAN_TMUX_SCHEMA, "main_ui")
    _register_alias("browser", "browser", VULCAN_BROWSER_SCHEMA, "main_ui")
    _register_alias("gui", "gui", VULCAN_GUI_SCHEMA, "main_ui")
    _register_alias("web", "web", VULCAN_WEB_SCHEMA, "main_ui")
    _register_alias("workspace", "workspace", VULCAN_WORKSPACE_SCHEMA, "main_ui")
    _register_alias("workspace_ui", "workspace_ui", VULCAN_WORKSPACE_UI_SCHEMA, "main_ui")

if _BRIDGE_PROFILE == "open_manus":
    _register_alias("read", "read", VULCAN_READ_SCHEMA, "open_manus")
    _register_alias("write", "write", VULCAN_WRITE_SCHEMA, "open_manus")
    _register_alias("edit", "edit", VULCAN_EDIT_SCHEMA, "open_manus")
    _register_alias("bash", "bash", VULCAN_BASH_SCHEMA, "open_manus")
    _register_alias("todo", "todo", VULCAN_TODO_SCHEMA, "open_manus")
    _register_alias("sandboxed_iex", "sandboxed_iex", VULCAN_SANDBOXED_IEX_SCHEMA, "open_manus")
    _register_alias("tmux", "tmux", VULCAN_TMUX_SCHEMA, "open_manus")
    _register_alias("browser", "browser", VULCAN_BROWSER_SCHEMA, "open_manus")
    _register_alias("gui", "gui", VULCAN_GUI_SCHEMA, "open_manus")
    _register_alias("web", "web", VULCAN_WEB_SCHEMA, "open_manus")
    _register_alias("manus_ui", "manus_ui", VULCAN_MANUS_UI_SCHEMA, "open_manus")
    _register_alias(
        "reset_container",
        "reset_container",
        VULCAN_RESET_CONTAINER_SCHEMA,
        "open_manus",
    )
    _register_alias(
        "project_skill",
        "project_skill",
        VULCAN_PROJECT_SKILL_SCHEMA,
        "open_manus",
    )
    _register_alias(
        "shared_skill",
        "shared_skill",
        VULCAN_SHARED_SKILL_SCHEMA,
        "open_manus",
    )
