"""Sharing recommendations preserve ordering, saved evidence and consent stages."""
import json

import pytest

from gateway.wisdom_command import WisdomCommandContext, bind_view_callbacks
from hermes_wisdom.mediation_view import advice_view, interaction_view
from plugins.platforms.telegram.wisdom_adapter import TelegramWisdomMixin
from plugins.platforms.slack.wisdom_blocks import render_wisdom_blocks


RATIONALE = "Hermes thinks the following skill would be useful to the rest of your team:"


def candidate(*, operation="share", portal=False):
    return {
        "id": "exact-package", "operation": operation, "state": "pending",
        "actions": ["defer", "inspect", "confirm"],
        "facts": {"editorial_name": "Team notes", "editorial_description": "Record decisions.",
                  "local_version": "1.0", "file_names": ["SKILL.md"]},
        "result": {"portal_url": "https://portal.example/private/review"} if portal else {},
    }


def recommendation(current, *, explanation=None):
    return {"assessment": {"reference": {"kind": "candidate"}}, "interaction": current,
            "advice": {"title": "Team notes", "relevance": "recommend", "assessment_kind": "qualification",
                       "explanation": explanation or "Hermes identified this local skill as a sharing candidate."}}


def test_qualification_rationale_precedes_title_in_every_serializer():
    current = candidate()
    view = advice_view([recommendation(current)], introduction=True)
    context = WisdomCommandContext("u", "c", None, "org")
    bind_view_callbacks(view, context)
    for rendered in (view.to_text(), view.to_local_text(),
                     TelegramWisdomMixin._wisdom_command_html(view, full_details=True),
                     TelegramWisdomMixin._wisdom_command_text(view),
                     json.dumps(render_wisdom_blocks(view), ensure_ascii=False)):
        assert RATIONALE in rendered
        assert rendered.index(RATIONALE) < rendered.index("Team notes")
        assert "identified this local skill" not in rendered
        assert rendered.count(RATIONALE) == 1
        assert rendered.index("Record decisions.") < rendered.index("Would you like to share it?")


def passing_facts(*, pending_gateway=False):
    from hermes_wisdom.local_security import prepared_security_check
    from hermes_wisdom.professionalism import CHECK_KEYS

    return {
        "security_check": prepared_security_check(
            [], "Example description", {"guard": {"allowed": True}},
            include_gateway_pending=pending_gateway,
        ),
        "professionalism_check": {
            "status": "pass", "checks": [
                {"key": key, "status": "pass", "finding_count": 0, "details": []}
                for key in CHECK_KEYS
            ],
        },
    }


def test_passing_summary_collapses_and_expands_all_six_saved_checks():
    from hermes_wisdom.review_presentation import review_card_text

    facts = passing_facts()
    assert review_card_text(facts) == "✅ Safe To Share (security and professionalism checks all passed)"
    expanded = review_card_text(facts, expanded=True)
    assert expanded.splitlines()[0] == "✅ Safe To Share"
    checks = [line for line in expanded.splitlines()[1:] if "✅" in line]
    assert len(checks) == 6
    assert all(line.startswith("  ✅") for line in checks)
    assert "security certification" in expanded


@pytest.mark.parametrize("defect", ["pending_gateway", "missing_security", "missing_professionalism",
    "missing_security_rows", "missing_professionalism_rows", "partial_security", "partial_professionalism",
    "advisory", "blocked", "unavailable", "pending", "running", "retry", "row_advisory", "row_finding",
    "missing_count", "local_unavailable"])
def test_no_safe_summary_for_incomplete_or_nonpassing_evidence(defect):
    from hermes_wisdom.review_presentation import review_card_text

    facts = passing_facts(pending_gateway=defect == "pending_gateway")
    security = facts["security_check"]
    professional = facts["professionalism_check"]
    if defect.startswith("missing_") and defect in {"missing_security", "missing_professionalism"}:
        facts.pop("security_check" if defect == "missing_security" else "professionalism_check")
    elif defect == "missing_security_rows":
        security.pop("checks")
    elif defect == "missing_professionalism_rows":
        professional.pop("checks")
    elif defect == "partial_security":
        security["checks"].pop()
    elif defect == "partial_professionalism":
        professional["checks"].pop()
    elif defect in {"advisory", "blocked", "unavailable", "pending", "running", "retry"}:
        security["status"] = defect
    elif defect == "row_advisory":
        professional["checks"][0]["status"] = "advisory"
    elif defect == "row_finding":
        security["checks"][0]["finding_count"] = 1
    elif defect == "missing_count":
        security["checks"][0].pop("finding_count")
    elif defect == "local_unavailable":
        security["local_status"] = "unavailable"
    for expanded in (False, True):
        text = review_card_text(facts, expanded=expanded)
        assert "Safe To Share" not in text
        assert "all passed" not in text
        if defect == "pending_gateway":
            assert "Organization policy: Pending" in text
            assert "Personal information: Pending" in text


@pytest.mark.parametrize("stage", ["initial", "weekly", "local", "portal"])
@pytest.mark.parametrize("expanded", [False, True])
def test_sharing_action_bar_order_and_stage_safe_targets(stage, expanded):
    current = candidate(operation="publish" if stage in {"local", "portal"} else "share", portal=stage == "portal")
    current["facts"].update(passing_facts(pending_gateway=stage == "local"))
    if stage in {"initial", "weekly"}:
        item = recommendation(current, explanation="Used across several days." if stage == "weekly" else None)
        view = advice_view([item], checks_expanded=expanded)
    else:
        view = interaction_view(current, checks_expanded=expanded)
    actions = [a for item in view.items for a in item.actions] + view.actions
    labels = [a.label for a in actions]
    assert labels == ["Review Checks" if stage == "local" else "✅ Safe To Share",
                      "View More Details", "Snooze Collective Wisdom", "Maybe Later", "Share My Skill"]
    assert actions[0].callback_data == f"wi:agent:checks.{'hide' if expanded else 'show'}:exact-package"
    assert actions[2].operation == "mute" and actions[2].local_command == "/wisdom mute"
    assert actions[3].callback_data == "wi:agent:defer:exact-package"
    assert actions[4].callback_data == "wi:agent:confirm:exact-package" and actions[4].primary
    if stage == "portal":
        assert actions[1].url == current["result"]["portal_url"]
    else:
        assert not any(a.url for a in actions)
        assert actions[1].callback_data == f"wi:agent:{'inspect.0' if stage == 'local' else 'review'}:exact-package"
        text = view.to_text()
        assert "View More Details" in text
        if stage == "local":
            assert "without uploading" in text
        else:
            assert "authorizes" in text and "private Portal draft" in text
            assert "Share My Skill prepares" in text
    bind_view_callbacks(view, WisdomCommandContext("u", "c", None, "org"))
    keyboard = TelegramWisdomMixin._wisdom_command_keyboard(view)
    assert [b.text for row in keyboard.inline_keyboard for b in row] == labels
    assert all(len(row) <= 2 for row in keyboard.inline_keyboard)
    blocks = render_wisdom_blocks(view)
    assert [b["text"]["text"] for block in blocks if block["type"] == "actions" for b in block["elements"]] == labels
    html = TelegramWisdomMixin._wisdom_command_html(view, full_details=True)
    local = view.to_local_text()
    for rendered in (html, local):
        positions = [rendered.rindex(label) for label in labels]
        assert positions == sorted(positions)
        assert rendered.count("Snooze Collective Wisdom") == 1


@pytest.mark.parametrize("state", ["stale", "expired", "applying", "needs_review", "deferred"])
def test_nonactionable_cards_never_claim_current_safe_sharing(state):
    current = candidate(operation="publish", portal=True)
    current["facts"].update(passing_facts())
    current["state"] = "pending" if state == "deferred" else state
    current["deferred"] = state == "deferred"
    for view in (interaction_view(current), advice_view([recommendation(current)])):
        actions = [a for item in view.items for a in item.actions] + view.actions
        assert not any(a.primary for a in actions)
        assert "Share My Skill" not in [a.label for a in actions]
        assert "Safe To Share" not in view.to_text()
        assert "ready for sharing" not in view.to_text()


def gateway_facts():
    facts = passing_facts()
    # Gateway schema is distinct from local preflight; no skills-guard result is supplied.
    facts["security_check"] = {
        "schema_version": 1, "status": "pass", "summary": "No known matches detected.",
        "checks": [{"key": key, "label": label, "status": "pass", "finding_count": 0, "details": []}
                   for key, label in (("private_keys", "Private keys"), ("live_credentials", "Live credentials"),
                       ("organization_policy", "Organization policy"), ("personal_information", "Personal information"),
                       ("secret_like_assignments", "Secret-like assignments"))],
    }
    return facts


def test_gateway_checks_use_their_own_complete_schema_without_inventing_local_guard():
    from hermes_wisdom.review_presentation import review_card_text

    facts = gateway_facts()
    assert "Safe To Share" in review_card_text(facts)
    assert "harmful instruction" not in review_card_text(facts, expanded=True).lower()
    facts["security_check"]["checks"].pop()
    assert "Safe To Share" not in review_card_text(facts)


def test_unavailable_assessment_never_claims_safe_or_ready():
    current = candidate(operation="publish", portal=True)
    current["facts"].update(passing_facts())
    item = recommendation(current)
    item["advice"]["assessment_status"] = "unavailable"
    view = advice_view([item])
    assert "Safe To Share" not in view.to_text()
    assert "Safe To Share" not in str([a.label for a in view.items[0].actions])
    assert not any(a.primary for a in view.items[0].actions)


def test_rich_sharing_controls_wrap_in_source_order_without_narrow_five_button_row():
    import re

    current = candidate()
    view = bind_view_callbacks(interaction_view(current), WisdomCommandContext("u", "c", None, "org"))
    html = TelegramWisdomMixin._wisdom_command_html(view, full_details=True)
    rows = re.findall(r"<tg-button-row[^>]*>(.*?)</tg-button-row>", html)
    assert all(row.count("<tg-button ") <= 2 for row in rows)
    assert len(rows) == 3


def test_expanded_check_indentation_survives_html_whitespace_collapse():
    current = candidate()
    current["facts"].update(passing_facts())
    view = interaction_view(current, checks_expanded=True)
    html = TelegramWisdomMixin._wisdom_command_html(view, full_details=True)
    assert html.count("<br/>&nbsp;&nbsp;✅") == 6
