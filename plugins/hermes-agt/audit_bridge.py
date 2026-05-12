"""Dual-write audit bridge: AGT AuditLog + Hermes agent.log.

Every policy decision and tool outcome is appended to:

  1. AGT's Merkle-chained audit log (so ``agt verify --evidence`` works).
  2. Hermes's agent.log via the standard logging facility (so existing
     log tooling — ``hermes logs``, grep, etc. — keeps working).

Both writes share a correlation id so events can be cross-referenced.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_agt.audit")

# We don't want a hard dependency on AGT at import time — the plugin must
# load (and degrade gracefully) when AGT isn't installed. The bridge keeps
# a lazy reference to the AGT AuditLog instance.
_audit_log: Optional[Any] = None
_audit_path: Optional[Path] = None


def init(audit_path: Optional[str] = None) -> None:
    """Initialise the AGT AuditLog. Idempotent."""
    global _audit_log, _audit_path
    if _audit_log is not None:
        return

    try:
        from agentmesh.governance.audit import AuditLog
    except Exception as exc:
        logger.debug("hermes-agt: AGT AuditLog unavailable (%s); JSONL-only mode", exc)
        AuditLog = None  # type: ignore

    path = Path(audit_path).expanduser() if audit_path else (
        Path(os.path.expanduser("~/.hermes")) / "agt-audit.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _audit_path = path

    if AuditLog is not None:
        try:
            # AuditLog construction signature varies across AGT versions.
            # Try the simple no-arg form first; fall back to no-init.
            _audit_log = AuditLog()
        except Exception as exc:
            logger.debug("hermes-agt: AuditLog() instantiation failed (%s); "
                         "falling back to JSONL-only", exc)
            _audit_log = None

    logger.info("hermes-agt: audit bridge initialised at %s", path)


def correlation_id() -> str:
    """Fresh correlation id for a single tool-call lifecycle."""
    return uuid.uuid4().hex[:16]


def write_decision(
    *,
    correlation: str,
    tool_name: str,
    decision: str,            # "allow" | "deny" | "audit"
    matched_rule: Optional[str],
    context: Dict[str, Any],
    requires_review: bool = False,
    trust_score: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a policy decision to both audit sinks."""
    entry = {
        "ts": _now_iso(),
        "correlation_id": correlation,
        "kind": "policy_decision",
        "tool_name": tool_name,
        "decision": decision,
        "matched_rule": matched_rule,
        "requires_review": requires_review,
        "trust_score": trust_score,
        "context": _safe_context(context),
    }
    if extra:
        entry.update(extra)
    _emit(entry)


def write_outcome(
    *,
    correlation: str,
    tool_name: str,
    duration_ms: int,
    error: bool,
    error_message: Optional[str] = None,
    result_preview: Optional[str] = None,
) -> None:
    """Record a tool execution outcome."""
    entry = {
        "ts": _now_iso(),
        "correlation_id": correlation,
        "kind": "tool_outcome",
        "tool_name": tool_name,
        "duration_ms": duration_ms,
        "error": error,
        "error_message": error_message,
        "result_preview": (result_preview or "")[:240],
    }
    _emit(entry)


def write_approval_resolution(
    *,
    correlation: str,
    tool_name: str,
    surface: str,        # "cli" | "gateway"
    choice: str,         # "once" | "session" | "always" | "deny" | "timeout"
) -> None:
    entry = {
        "ts": _now_iso(),
        "correlation_id": correlation,
        "kind": "approval_resolution",
        "tool_name": tool_name,
        "surface": surface,
        "choice": choice,
    }
    _emit(entry)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _emit(entry: Dict[str, Any]) -> None:
    # 1. JSONL sink (always)
    if _audit_path is not None:
        try:
            with open(_audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            logger.debug("hermes-agt: jsonl write failed: %s", exc)

    # 2. AGT AuditLog (if available)
    if _audit_log is not None:
        try:
            # AuditLog APIs vary; try the most common shapes.
            if hasattr(_audit_log, "append"):
                _audit_log.append(entry)
            elif hasattr(_audit_log, "log"):
                _audit_log.log(**entry)
            elif hasattr(_audit_log, "record"):
                _audit_log.record(entry)
        except Exception as exc:
            logger.debug("hermes-agt: AGT AuditLog write failed: %s", exc)

    # 3. Hermes agent.log via standard logging
    level = logging.WARNING if entry.get("decision") == "deny" or entry.get("error") else logging.INFO
    logger.log(level, "hermes-agt audit: %s",
               json.dumps({k: v for k, v in entry.items() if k != "context"},
                          ensure_ascii=False, default=str))


def _safe_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Strip noisy / large fields before audit serialisation."""
    safe = dict(ctx) if isinstance(ctx, dict) else {}
    # Truncate any string longer than 500 chars
    for k, v in list(safe.items()):
        if isinstance(v, str) and len(v) > 500:
            safe[k] = v[:497] + "..."
    return safe


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
