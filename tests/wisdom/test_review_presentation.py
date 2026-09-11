from hermes_wisdom.review_presentation import (
    aggregate_review_text,
    full_review_text,
    review_status_text,
    review_check_line,
    review_summary_text,
)
import pytest


def test_clean_summary_copy_preserves_findings_and_safety_disclaimer():
    assert review_summary_text("No known matches detected.") == "No issues detected"
    assert review_summary_text("Review the policy finding.") == "Review the policy finding."
    text = full_review_text({"status": "pass", "summary": "No known matches detected."}, None)
    assert "No issues detected" in text
    assert "These checks are not a security certification." in text
    gateway_note = " Required Gateway checks run after you authorize upload and before publication."
    assert review_summary_text("Local security checks found no known matches." + gateway_note) == (
        "No issues detected by local security checks." + gateway_note
    )


@pytest.mark.parametrize("status,expected", [
    ("pass", "✅ No security issues detected"),
    ("advisory", "⚠️ Security check: Advisory"),
    ("blocked", "❌ Security check: Blocked"),
    ("pending", "⏳ Security check: Pending"),
    ("unavailable", "➖ Security check: Unavailable"),
])
def test_card_status_leads_with_icon_and_keeps_nonpass_meaning(status, expected):
    assert review_check_line("Security check", status) == expected


def test_expanded_card_rows_keep_summary_and_findings_without_pass_labels():
    text = full_review_text({
        "status": "advisory", "summary": "Review a policy match.",
        "checks": [{"label": "Private keys", "status": "pass"},
                   {"label": "Organization policy", "status": "advisory",
                    "finding_count": 1, "details": ["Policy match details"]}],
    }, {"status": "pass"})
    assert "✅ No private keys detected" in text
    assert "⚠️ Organization policy: Advisory (1 finding)" in text
    assert "Review a policy match." in text
    assert "Policy match details" in text
    assert "Pass" not in text


def test_review_status_text_covers_every_presented_state():
    assert review_status_text("pass") == "✅ Pass"
    assert review_status_text("advisory") == "⚠️ Advisory"
    assert review_status_text("blocked") == "❌ Blocked"
    assert review_status_text("pending") == "⏳ Pending"
    assert review_status_text("running") == "⏳ Pending"
    assert review_status_text("retry") == "⏳ Pending"
    assert review_status_text("unavailable") == "➖ Unavailable"
    assert review_status_text("future-status") == "➖ Unavailable"


def test_full_review_text_renders_each_check_as_its_own_labeled_row():
    rendered = full_review_text(
        {
            "status": "advisory",
            "summary": "One review item needs attention.",
            "checks": [
                {
                    "key": "private_keys",
                    "label": "Private keys",
                    "status": "pass",
                    "finding_count": 0,
                    "details": [],
                },
                {
                    "key": "organization_policy",
                    "label": "Organization policy",
                    "status": "advisory",
                    "finding_count": 1,
                    "details": ["Review a bounded policy match."],
                },
            ],
        },
        {
            "status": "blocked",
            "checks": [
                {
                    "key": "profanity_or_abuse",
                    "status": "blocked",
                    "finding_count": 1,
                    "details": ["Potential abusive language."],
                }
            ],
        },
    )

    assert rendered.splitlines() == [
        "⚠️ Security check: Advisory",
        "One review item needs attention.",
        "✅ No private keys detected",
        "⚠️ Organization policy: Advisory (1 finding)",
        "  Review a bounded policy match.",
        "These checks are not a security certification.",
        "",
        "❌ Professionalism check (agent-assessed, advisory): Blocked",
        "❌ Profanity or abusive language: Blocked (1 finding)",
        "  Potential abusive language.",
    ]


def test_public_review_projection_remains_aggregate_only():
    rendered = aggregate_review_text(
        {"status": "pass", "checks": [{"label": "Private keys"}]},
        {"status": "unavailable", "checks": [{"label": "Hate or harassment"}]},
    )

    assert rendered == "✅ No security issues detected · ➖ Professionalism check (agent-assessed, advisory): Unavailable"
    assert "Private keys" not in rendered
    assert "Hate or harassment" not in rendered


@pytest.mark.parametrize("status", ["pass", "advisory", "blocked", "pending", "running", "retry", "unavailable", "future-status"])
def test_browse_and_version_summaries_preserve_nonpass_state_without_private_findings(status):
    security = {"status": status, "checks": [{"label": "Private keys", "details": ["PRIVATE_DETAIL"]}]}
    rendered = aggregate_review_text(security, {
        "status": status, "summary": "PRIVATE_SUMMARY", "checks": security["checks"],
    })
    assert rendered.startswith(review_check_line("Security check", status))
    assert "PRIVATE_DETAIL" not in rendered and "Private keys" not in rendered
    assert "PRIVATE_SUMMARY" not in rendered
    assert "Pass" not in rendered
    if status != "pass":
        assert not rendered.startswith("✅")


@pytest.mark.parametrize("status", ["pass", "advisory"])
def test_native_and_fixed_professionalism_copy_agree(status):
    from hermes_wisdom.review_presentation import review_card_text
    from hermes_wisdom.professionalism import review_text

    review = {
        "status": status,
        "checks": [
            {"key": "profanity_or_abuse", "status": "pass", "finding_count": 0},
            {"key": "hate_or_harassment", "status": status,
             "finding_count": 1 if status == "advisory" else 0},
        ],
    }
    for expanded in (False, True):
        native = review_card_text({
            "security_check": {"status": "pass"},
            "professionalism_check": review,
        }, expanded)
        assert native.endswith(review_text(review, include_checks=expanded))
        assert "Profanity or abusive language" not in native


def test_compact_checks_use_plain_passing_rows_without_losing_nonpass_facts():
    from hermes_wisdom.review_presentation import review_card_text

    security = {"status": "pass", "source": "local_preflight", "local_status": "pass",
                "summary": "Local security checks found no known matches.",
                "checks": [{"label": label, "status": "pass", "details": ["No issues detected by this local check."]}
                           for label in ("Private keys", "Live credentials", "Secret-like assignments", "Harmful instruction patterns")]}
    facts = {"security_check": security, "professionalism_check": {"status": "pass"}}
    assert review_card_text(facts).splitlines() == [
        "✅ No security issues detected", "✅ No private keys detected",
        "✅ No live credentials detected", "✅ No secret key assignments",
        "✅ No harmful instruction patterns", "✅ Safe for work (no inappropriate content detected)",
    ]
    security.update(status="blocked", local_status="blocked", summary="Remove the detected credential.")
    security["checks"][1].update(status="blocked", finding_count=1, details=["Credential on line 2"])
    facts["professionalism_check"] = {"status": "unavailable"}
    text = review_card_text(facts)
    assert "❌ Security check (local preflight): Blocked" in text
    assert "Remove the detected credential." in text
    assert "❌ Live credentials: Blocked (1 finding)" in text
    assert "Credential on line 2" in text
    assert "No security issues detected" not in text
    assert "No live credentials detected" not in text
    assert "Safe for work" not in text
    assert "Unavailable" in text
    assert "security certification" not in text
    assert "security certification" in full_review_text(security, None)
