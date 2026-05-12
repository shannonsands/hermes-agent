"""Load AGT policies from disk into a PolicyEvaluator.

We load every ``*.yaml`` file under ``policy_dir`` and merge them into a
single PolicyEvaluator. PolicyDocument names must be unique across files.

Schema is the canonical AGT YAML policy schema (see
agent-governance-toolkit/templates/policies/starters/general-saas.yaml for
a worked example).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def load_policies(policy_dir: Path) -> "Optional['PolicyEvaluator']":
    """Build a PolicyEvaluator from every YAML file in ``policy_dir``.

    Returns ``None`` if AGT isn't installed, the directory doesn't exist,
    or no policy files were found. Caller should treat ``None`` as
    "fail-open" and let the call through unmodified.
    """
    try:
        from agent_os.policies.evaluator import PolicyEvaluator
        from agent_os.policies import (
            PolicyDocument, PolicyRule, PolicyCondition,
            PolicyAction, PolicyOperator, PolicyDefaults,
        )
    except Exception as exc:
        logger.warning("hermes-agt: AGT not installed (%s); plugin will be inert. "
                       "Install with: uv pip install --python <hermes-venv> "
                       "'agent-governance-toolkit[full]'", exc)
        return None

    policy_dir = Path(policy_dir).expanduser()
    if not policy_dir.is_dir():
        logger.info("hermes-agt: policy dir %s not found; using built-in default", policy_dir)
        # Fall back to the plugin's bundled default
        bundled = Path(__file__).parent / "policies"
        if bundled.is_dir():
            policy_dir = bundled
        else:
            return None

    import yaml

    docs: List["PolicyDocument"] = []
    for yaml_path in sorted(policy_dir.glob("*.yaml")):
        try:
            with open(yaml_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except Exception as exc:
            logger.warning("hermes-agt: failed to parse %s: %s", yaml_path, exc)
            continue
        if not raw or not isinstance(raw, dict):
            continue
        try:
            doc = _build_document(raw, PolicyDocument, PolicyRule, PolicyCondition,
                                  PolicyAction, PolicyOperator, PolicyDefaults)
            docs.append(doc)
            logger.debug("hermes-agt: loaded policy %s (%d rules)",
                         doc.name, len(doc.rules))
        except Exception as exc:
            logger.warning("hermes-agt: invalid policy in %s: %s", yaml_path, exc)
            continue

    if not docs:
        logger.info("hermes-agt: no valid policies in %s; plugin will be observe-only", policy_dir)
        return None

    return PolicyEvaluator(policies=docs)


def _build_document(raw, PolicyDocument, PolicyRule, PolicyCondition,
                    PolicyAction, PolicyOperator, PolicyDefaults):
    """Translate raw YAML dict to AGT dataclasses."""
    name = raw.get("name", "unnamed")
    version = str(raw.get("version", "1.0"))

    defaults_raw = raw.get("defaults", {}) or {}
    default_action = _coerce_action(defaults_raw.get("action", "allow"), PolicyAction)
    defaults = PolicyDefaults(action=default_action)

    rules = []
    for rd in raw.get("rules", []) or []:
        cond_raw = rd.get("condition", {}) or {}
        cond = PolicyCondition(
            field=cond_raw.get("field", "tool_name"),
            operator=_coerce_operator(cond_raw.get("operator", "eq"), PolicyOperator),
            value=cond_raw.get("value", ""),
        )
        rules.append(PolicyRule(
            name=rd.get("name", "anonymous"),
            condition=cond,
            action=_coerce_action(rd.get("action", "deny"), PolicyAction),
            priority=int(rd.get("priority", 0)),
        ))

    return PolicyDocument(
        name=name, version=version, defaults=defaults, rules=rules,
    )


def _coerce_action(value, PolicyAction):
    """Accept str or PolicyAction member, return PolicyAction.

    We extend AGT's enum vocabulary with one alias: ``review``. Since
    AGT's PolicyAction is {ALLOW, DENY, AUDIT, BLOCK}, we map the
    Hermes ``review`` keyword onto ``AUDIT`` — which AGT defines as
    "let it through but flag it for inspection." The interceptor
    treats AUDIT verdicts as "fall through to Hermes's existing
    approval UX" (CLI prompt, gateway buttons, smart-mode aux LLM)
    rather than auto-allowing.
    """
    if hasattr(value, "value"):
        return value
    s = str(value).strip().upper().replace("-", "_")
    # Map common aliases. REVIEW/PROMPT/WARN -> AUDIT (review-required).
    aliases = {"REVIEW": "AUDIT", "PROMPT": "AUDIT", "WARN": "AUDIT"}
    s = aliases.get(s, s)
    try:
        return PolicyAction[s]
    except KeyError as exc:
        raise ValueError(f"unknown PolicyAction: {value!r}") from exc


def _coerce_operator(value, PolicyOperator):
    if hasattr(value, "value"):
        return value
    s = str(value).strip().upper().replace("-", "_")
    aliases = {
        "EQUALS": "EQ", "==": "EQ", "=": "EQ",
        "NOT_EQUALS": "NE", "!=": "NE",
        "GREATER_THAN": "GT", ">": "GT",
        "LESS_THAN": "LT", "<": "LT",
        "GREATER_OR_EQUAL": "GTE", ">=": "GTE",
        "LESS_OR_EQUAL": "LTE", "<=": "LTE",
        "STARTS_WITH": "MATCHES",  # AGT only has MATCHES (regex); use ^prefix
        "ENDS_WITH": "MATCHES",
        "REGEX": "MATCHES",
    }
    s = aliases.get(s, s)
    try:
        return PolicyOperator[s]
    except KeyError as exc:
        raise ValueError(f"unknown PolicyOperator: {value!r}") from exc
