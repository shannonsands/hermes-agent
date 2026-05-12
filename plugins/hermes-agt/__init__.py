"""hermes-agt — Microsoft Agent Governance Toolkit (AGT) integration for Hermes.

PoC plugin. See README.md for usage. The plugin is fail-safe: if AGT isn't
installed or no policies are loaded, it runs in observe-only mode and never
blocks anything.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .interceptor import HermesGovernanceInterceptor

logger = logging.getLogger(__name__)

_INTERCEPTOR: HermesGovernanceInterceptor | None = None


def _config(ctx: Any, key: str, default: Any) -> Any:
    """Pull hermes_agt.<key> from Hermes config, with default."""
    try:
        from hermes_cli.config import load_config, cfg_get
        return cfg_get(load_config(), "hermes_agt", key, default=default)
    except Exception:
        return default


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p)) if isinstance(p, str) else p


def _hermes_home() -> Path:
    """Return Hermes-home directory, falling back to ~/.hermes if the
    helper isn't importable (e.g. running outside a Hermes session)."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def register(ctx) -> None:
    """Hermes plugin entry point. Called once at plugin load."""
    global _INTERCEPTOR

    home = _hermes_home()
    policy_dir = Path(_expand(_config(ctx, "policy_dir", str(home / "policies"))))
    audit_path = _expand(_config(ctx, "audit_path", str(home / "agt-audit.jsonl")))
    default_action = _config(ctx, "default_action", "allow")
    fail_open = bool(_config(ctx, "fail_open", True))
    trust_cfg = _config(ctx, "trust", {}) or {}
    trust_enabled = bool(trust_cfg.get("enabled", True)) if isinstance(trust_cfg, dict) else True
    trust_threshold = float(trust_cfg.get("threshold", 0.5)) if isinstance(trust_cfg, dict) else 0.5
    trust_penalty = float(trust_cfg.get("deny_penalty", 0.15)) if isinstance(trust_cfg, dict) else 0.15

    _INTERCEPTOR = HermesGovernanceInterceptor(
        policy_dir=policy_dir,
        audit_path=audit_path,
        default_action=default_action,
        trust_threshold=trust_threshold,
        trust_deny_penalty=trust_penalty,
        trust_enabled=trust_enabled,
        fail_open=fail_open,
    )

    ctx.register_hook("pre_tool_call",          _INTERCEPTOR.on_pre_tool_call)
    ctx.register_hook("post_tool_call",         _INTERCEPTOR.on_post_tool_call)
    ctx.register_hook("pre_approval_request",   _INTERCEPTOR.on_pre_approval_request)
    ctx.register_hook("post_approval_response", _INTERCEPTOR.on_post_approval_response)
    ctx.register_hook("on_session_start",       _INTERCEPTOR.on_session_start)
    ctx.register_hook("on_session_end",         _INTERCEPTOR.on_session_end)

    mode = "active" if _INTERCEPTOR.evaluator is not None else "observe-only"
    logger.info("hermes-agt: registered (%s); policy_dir=%s, audit=%s",
                mode, policy_dir, audit_path)


def get_interceptor() -> HermesGovernanceInterceptor | None:
    """Test/diagnostics access to the live interceptor."""
    return _INTERCEPTOR
