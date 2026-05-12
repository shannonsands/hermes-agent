"""HermesGovernanceInterceptor — the core of the hermes-agt plugin.

Wires Hermes's pre_tool_call / post_tool_call / approval hooks into AGT's
PolicyEvaluator and AuditLog. Stays observe-only when AGT isn't installed
or no policies are loaded, so installing the plugin is always safe.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import audit_bridge
from .context import build_context
from .policy_loader import load_policies
from .trust import TrustTracker

logger = logging.getLogger(__name__)


class HermesGovernanceInterceptor:
    """Single-instance interceptor wired to Hermes plugin hooks."""

    def __init__(self,
                 *,
                 policy_dir: Path,
                 audit_path: str,
                 default_action: str = "allow",
                 trust_threshold: float = 0.5,
                 trust_deny_penalty: float = 0.15,
                 trust_enabled: bool = True,
                 fail_open: bool = True) -> None:
        self.policy_dir = policy_dir
        self.default_action = default_action.lower()
        self.fail_open = fail_open
        self.trust_enabled = trust_enabled

        # Per-call state keyed by tool_call_id, so we can correlate
        # pre_tool_call → post_tool_call → approval hooks.
        self._calls: Dict[str, Dict[str, Any]] = {}
        self._calls_lock = threading.Lock()

        self.trust = TrustTracker(threshold=trust_threshold,
                                  deny_penalty=trust_deny_penalty)

        # Lazy: PolicyEvaluator may be None if AGT isn't installed.
        self.evaluator = load_policies(policy_dir)
        if self.evaluator is None:
            logger.info("hermes-agt: running in observe-only mode "
                        "(no AGT or no policies loaded)")

        audit_bridge.init(audit_path)

    # ------------------------------------------------------------------
    # Hook callbacks
    # ------------------------------------------------------------------

    def on_pre_tool_call(self, *, tool_name: str = "", args: Any = None,
                         task_id: str = "", session_id: str = "",
                         tool_call_id: str = "", **_: Any):
        """Return ``{"action": "block", "message": "..."}`` to deny.

        Returning ``None`` lets the call through.
        """
        correlation = audit_bridge.correlation_id()
        ctx = build_context(
            tool_name=tool_name,
            args=args if isinstance(args, dict) else {},
            session_id=session_id,
            task_id=task_id,
        )

        # Capture per-call state for the matching post_tool_call event.
        call_state = {
            "correlation": correlation,
            "context": ctx,
            "started_at": time.monotonic(),
            "tool_name": tool_name,
        }
        with self._calls_lock:
            key = tool_call_id or f"{session_id}:{task_id}:{tool_name}:{time.monotonic_ns()}"
            self._calls[key] = call_state
        # Stash the synthesized key on the state itself so
        # post_tool_call can find it even when tool_call_id is empty.
        call_state["_key"] = key

        if self.evaluator is None:
            # Observe-only: log nothing here, post_tool_call will record
            # the outcome.
            return None

        try:
            result = self.evaluator.evaluate(ctx)
        except Exception as exc:
            logger.warning("hermes-agt: policy evaluation error: %s", exc)
            audit_bridge.write_decision(
                correlation=correlation,
                tool_name=tool_name,
                decision="error",
                matched_rule=None,
                context=ctx,
                extra={"error": str(exc)},
            )
            return None if self.fail_open else {
                "action": "block",
                "message": f"hermes-agt: policy evaluator error and fail_open=False: {exc}",
            }

        action_str = (getattr(result, "action", None) or "").lower() if not hasattr(
            result.action, "name") else result.action.name.lower()
        matched = getattr(result, "matched_rule", None) or getattr(result, "rule", None)
        allowed = bool(getattr(result, "allowed", action_str in ("allow", "audit")))

        # Trust handling: if trust below threshold, force review on this call
        # by treating it as a blocked-and-deferred case. For now, the PoC just
        # logs it — wiring into the Hermes approval UX comes in Phase C.
        forced_review = False
        if self.trust_enabled and allowed and self.trust.below_threshold(session_id):
            forced_review = True

        audit_bridge.write_decision(
            correlation=correlation,
            tool_name=tool_name,
            decision=action_str,
            matched_rule=matched,
            context=ctx,
            requires_review=forced_review,
            trust_score=self.trust.get(session_id) if self.trust_enabled else None,
        )

        if not allowed:
            # Deny path
            if self.trust_enabled:
                self.trust.penalize(session_id)
            reason = f"hermes-agt: denied by policy '{matched or 'default'}' " \
                     f"(rule action={action_str})"
            return {"action": "block", "message": reason}

        # Allow / audit path — let Hermes continue. If forced_review, the
        # tool runs but we've audit-logged the trust gate.
        return None

    def on_post_tool_call(self, *, tool_name: str = "", args: Any = None,
                          result: Any = None, task_id: str = "",
                          session_id: str = "", tool_call_id: str = "",
                          duration_ms: int = 0, **_: Any) -> None:
        # Look up the correlation we issued in pre_tool_call.
        correlation = ""
        with self._calls_lock:
            # Try the explicit tool_call_id first; fall back to most recent
            # entry for this tool.
            state = self._calls.pop(tool_call_id, None)
            if state is None:
                for k in list(self._calls.keys()):
                    if self._calls[k].get("tool_name") == tool_name:
                        state = self._calls.pop(k)
                        break
        if state:
            correlation = state.get("correlation", "")

        # Detect tool errors. Hermes wraps every tool result in a JSON envelope.
        # For native tools the shape is `{"output": "...", "exit_code": N,
        # "error": null|str}` — the "error" key is *always* present but is
        # `null` for success. For tool-side failures the envelope flips to
        # `{"error": "msg"}`. We need to distinguish: parse the JSON and look
        # at the actual values.
        is_error = False
        err_msg: Optional[str] = None
        preview = result if isinstance(result, str) else str(result)

        try:
            import json as _json
            parsed = _json.loads(result) if isinstance(result, str) else None
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            err_val = parsed.get("error")
            exit_code = parsed.get("exit_code")
            if err_val and not (err_val is None or err_val == "null"):
                is_error = True
                err_msg = str(err_val)[:400]
            elif isinstance(exit_code, int) and exit_code != 0:
                is_error = True
                err_msg = f"non-zero exit_code={exit_code}"

        audit_bridge.write_outcome(
            correlation=correlation or audit_bridge.correlation_id(),
            tool_name=tool_name,
            duration_ms=int(duration_ms or 0),
            error=is_error,
            error_message=err_msg,
            result_preview=preview,
        )

    # ------------------------------------------------------------------
    # Approval hooks (observers — Hermes still drives the UX)
    # ------------------------------------------------------------------

    def on_pre_approval_request(self, *, command: str = "", description: str = "",
                                pattern_key: str = "", session_key: str = "",
                                surface: str = "", **_: Any) -> None:
        # Observer only: AGT didn't drive this prompt (Hermes's existing
        # DANGEROUS_PATTERNS did), but we still want it in the AGT audit
        # so the trail is complete. Deeper integration (AGT decides when
        # to invoke the prompt) lands in Phase C.
        audit_bridge.write_decision(
            correlation=audit_bridge.correlation_id(),
            tool_name="terminal",
            decision="audit",
            matched_rule=f"hermes-pattern:{pattern_key}",
            context={"command": command, "surface": surface,
                     "session_key": session_key, "description": description},
            requires_review=True,
        )

    def on_post_approval_response(self, *, command: str = "", choice: str = "",
                                  surface: str = "", session_key: str = "",
                                  **_: Any) -> None:
        audit_bridge.write_approval_resolution(
            correlation=audit_bridge.correlation_id(),
            tool_name="terminal",
            surface=surface or "cli",
            choice=choice,
        )

        # Trust delta: explicit deny / timeout drops trust; "always" lifts it
        # slightly; "once" / "session" leave it where it is.
        if not self.trust_enabled:
            return
        if choice in {"deny", "timeout"}:
            self.trust.penalize(session_key)
        elif choice == "always":
            self.trust.adjust(session_key, +0.05)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def on_session_start(self, *, session_id: str = "", **_: Any) -> None:
        if self.trust_enabled:
            # Make sure the session has an initial entry.
            self.trust.get(session_id)

    def on_session_end(self, *, session_id: str = "", **_: Any) -> None:
        # Keep trust around in case the user resumes; reset only on
        # explicit /reset (which fires on_session_reset, not on_session_end).
        return
