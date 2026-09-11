import pytest

from hermes_wisdom.mediation_view import advice_view, interaction_view


def interaction(*, portal=True, operation="publish", state="pending", actions=None):
    return {"id": "exact-package", "operation": operation, "state": state,
            "actions": actions if actions is not None else ["defer", "inspect", "confirm"],
            "facts": {"editorial_name": "Team notes", "editorial_description": "Helps teammates record decisions.",
                      "file_names": ["SKILL.md"], "security_check": {"status": "pass"},
                      "professionalism_check": {"status": "pass"}},
            "result": {"portal_url": "https://portal.example/wisdom/review/draft"} if portal else {}}


def test_private_review_is_compact_and_actions_keep_exact_consent():
    current = interaction()
    view = interaction_view(current)
    assert view.summary == "Ready For Review"
    assert view.items[0].detail.startswith(current["facts"]["editorial_description"])
    assert "Safe To Share" not in view.to_text()  # Historical fixture lacks the full checks.
    assert [a.label for a in view.actions] == ["Review Checks", "View More Details", "Snooze Collective Wisdom", "Maybe Later", "Share My Skill"]
    assert view.actions[1].url == current["result"]["portal_url"]
    assert view.actions[3].callback_data == "wi:agent:defer:exact-package"
    assert view.actions[4].callback_data == "wi:agent:confirm:exact-package"
    assert view.actions[4].primary
    expanded = interaction_view(current, checks_expanded=True)
    assert "SKILL.md" in expanded.to_text()
    assert "security certification" in expanded.to_text()
    assert "Nothing changes until" not in expanded.to_text()


@pytest.mark.parametrize("state", ["pending", "stale", "expired", "applying"])
def test_blocked_or_unready_copy_never_offers_sharing(state):
    current = interaction(state=state, actions=["defer", "inspect"])
    current["facts"]["security_check"] = {"status": "blocked", "summary": "Remove a private key."}
    view = interaction_view(current)
    assert "Share My Skill" not in [a.label for a in view.actions]
    if state != "pending":
        assert view.summary != "Ready For Review"
    if state == "pending":
        assert "Remove a private key." in view.to_text()
        assert "No security issues detected" not in view.to_text()


def test_local_package_and_initial_suggestion_do_not_fake_portal_or_publish():
    local = interaction(portal=False)
    view = interaction_view(local)
    assert [a.label for a in view.actions] == ["Review Checks", "View More Details", "Snooze Collective Wisdom", "Maybe Later", "Share My Skill"]
    assert view.actions[1].callback_data == "wi:agent:inspect.0:exact-package"
    assert not any(a.url for a in view.actions)
    local["inspection"] = {"path": "SKILL.md", "page": 0, "page_count": 1, "content": "# Notes"}
    inspected = interaction_view(local)
    assert "# Notes" in inspected.to_text()
    assert "security certification" in inspected.to_text()
    assert inspected.actions[-1].callback_data == "wi:agent:confirm:exact-package"
    initial = interaction(portal=False, operation="share")
    view = interaction_view(initial)
    assert "Share My Skill" in [a.label for a in view.actions]
    assert "Share My Skill prepares" in view.to_text()
    assert "authorizes preparation and upload of a private Portal draft" in view.to_text()


def test_ready_advice_is_conversational_and_checks_are_factual():
    current = interaction(portal=False)
    item = {"assessment": {"reference": {"kind": "candidate", "prepared_draft_id": "local"}},
            "interaction": current, "advice": {"title": "Team notes", "explanation": "Would you like to share it?", "relevance": "recommend"}}
    view = advice_view([item])
    assert view.summary == "Your skill is ready for sharing."
    assert "Package facts" not in view.to_text()
    assert "Hermes recommendation" not in view.to_text()
    assert "✅ No security issues detected" in view.to_text()
    assert "✅ Safe for work (no inappropriate content detected)" in view.to_text()
    assert [a.label for a in view.items[0].actions] == ["Review Checks", "View More Details", "Snooze Collective Wisdom", "Maybe Later", "Share My Skill"]
    current["actions"] = ["defer", "inspect"]
    current["facts"]["security_check"] = {"status": "unavailable"}
    view = advice_view([item])
    assert view.summary != "Your skill is ready for sharing."
    assert "No security issues detected" not in view.to_text()
    assert "Share My Skill" not in [a.label for a in view.items[0].actions]


def test_preparing_and_success_receipts_describe_only_the_actual_stage():
    current = interaction(portal=False, operation="share", state="completed")
    current["result"] = {"packaging_state": "queued"}
    view = interaction_view(current)
    assert view.summary == "Preparing to share"
    assert view.items[0].detail == "Collective Wisdom is packaging up your skill so that it's shareable, and you will have a chance to review it before it gets shared."
    assert all(a.label != "Share My Skill" for a in view.actions)
    current = interaction(state="completed")
    current["result"]["publication_state"] = "published"
    view = interaction_view(current)
    assert view.summary == "Shared!"
    assert view.items[0].detail == "Your skill is now shared with your team. Thank you for contributing to your organization's collective wisdom."
    assert [a.label for a in view.actions] == ["View details"]
    current["result"]["publication_state"] = "pending_moderation"
    view = interaction_view(current)
    assert view.summary == "Pending moderation"
    assert "now shared" not in view.to_text()
    assert view.actions[0].label == "View details"
