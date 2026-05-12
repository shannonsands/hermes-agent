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

        # AUDIT verdict means "review required" — the policy author wrote
        # `action: review` (or `audit` directly) and wants a human to
        # approve before the call runs.
        is_review = action_str == "audit"

        # Trust handling: if trust below threshold, force review on this call
        # even when the rule itself said allow.
        forced_review = False
        if self.trust_enabled and allowed and not is_review and self.trust.below_threshold(session_id):
            forced_review = True

        audit_bridge.write_decision(
            correlation=correlation,
            tool_name=tool_name,
            decision=action_str,
            matched_rule=matched,
            context=ctx,
            requires_review=is_review or forced_review,
            trust_score=self.trust.get(session_id) if self.trust_enabled else None,
        )

        if not allowed:
            # Hard deny path
            if self.trust_enabled:
                self.trust.penalize(session_id)
            reason = f"hermes-agt: denied by policy '{matched or 'default'}' " \
                     f"(rule action={action_str})"
            return {"action": "block", "message": reason}

        # Review path: rule fired with `audit`/`review` action, OR trust
        # dropped below threshold on a normally-allowed call. Hand off to
        # Hermes's existing approval UX.
        if is_review or forced_review:
            return self._handle_review(
                tool_name=tool_name,
                ctx=ctx,
                matched_rule=matched or "trust-floor",
                correlation=correlation,
                session_id=session_id,
                forced_by_trust=forced_review,
            )

        # Plain allow path
        return None

    def _handle_review(self, *, tool_name: str, ctx: Dict[str, Any],
                       matched_rule: str, correlation: str,
                       session_id: str, forced_by_trust: bool) -> Optional[Dict[str, Any]]:
        """Prompt the user (CLI or gateway) to approve a review-flagged call.

        Honors per-session and permanent allowlists keyed on the matched
        rule name, so a 'session' or 'always' choice doesn't re-prompt for
        the same rule on subsequent calls.
        """
        # Per-session and permanent allowlists, keyed on matched_rule.
        # We use the rule name itself as the approval pattern_key so multiple
        # tools tripping the same rule share an approval.
        try:
            from tools.approval import (
                is_approved, approve_session, approve_permanent,
                prompt_dangerous_approval, get_current_session_key,
            )
        except Exception as exc:
            # Hermes approval module not importable from this context
            # (standalone smoke runs, certain test paths). Fall back to
            # treating review as allow + trust-penalty — better than
            # blocking outright when the UX isn't reachable.
            logger.debug("hermes-agt: approval module unavailable (%s); "
                         "letting review-flagged call through with trust penalty", exc)
            if self.trust_enabled:
                self.trust.adjust(session_id, -0.05)
            return None

        approval_key = matched_rule
        sess_key = get_current_session_key(default=session_id or "default")

        if is_approved(sess_key, approval_key):
            audit_bridge.write_approval_resolution(
                correlation=correlation,
                tool_name=tool_name,
                surface="cached",
                choice="session-allowlisted",
            )
            return None

        # Build a human-readable description for the prompt.
        cmd_or_path = (ctx.get("command") or ctx.get("path") or
                       ctx.get("code_first_line") or tool_name)
        if forced_by_trust:
            description = (f"trust score below threshold "
                           f"({self.trust.get(session_id):.2f}); "
                           f"reviewing {tool_name} call")
        else:
            description = f"AGT policy '{matched_rule}' requires human review"

        # Resolve the approval-callback registered by the active CLI / gateway.
        approval_callback = None
        try:
            from tools.terminal_tool import _approval_callback as _term_cb
            approval_callback = _term_cb
        except Exception:
            pass

        # Test/automation override: HERMES_AGT_AUTO_APPROVE = "once" / "session"
        # / "always" / "deny" forces the response without prompting. Used by
        # the smoke test, automated demos, and CI runs that need
        # deterministic behavior.
        _override = os.environ.get("HERMES_AGT_AUTO_APPROVE", "").strip().lower()
        if _override in {"once", "session", "always", "deny"}:
            choice = _override
        else:
            try:
                choice = prompt_dangerous_approval(
                    command=str(cmd_or_path),
                    description=description,
                    allow_permanent=True,
                    approval_callback=approval_callback,
                )
            except Exception as exc:
                logger.warning("hermes-agt: approval prompt failed: %s", exc)
                choice = "deny"

        audit_bridge.write_approval_resolution(
            correlation=correlation,
            tool_name=tool_name,
            surface="cli",
            choice=choice,
        )

        if choice in {"once"}:
            return None  # let through, will re-prompt next time
        if choice == "session":
            try:
                approve_session(sess_key, approval_key)
            except Exception:
                pass
            return None
        if choice == "always":
            try:
                approve_permanent(approval_key)
            except Exception:
                pass
            return None

        # deny / timeout / unknown
        if self.trust_enabled:
            self.trust.penalize(session_id)
        return {
            "action": "block",
            "message": f"hermes-agt: review denied for policy "
                       f"'{matched_rule}' (user choice={choice})",
        }

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
