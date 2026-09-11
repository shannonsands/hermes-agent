"""Product contracts for concise, stage-accurate recommendation cards."""
from hermes_wisdom.mediation_view import advice_view, interaction_view
from tests.wisdom.test_sharing_gallery import candidate, recommendation
from tests.wisdom.test_share_queue import sharing  # noqa: F401
from tests.wisdom.test_share_staging import staged  # noqa: F401
from tests.wisdom.test_feed_mediation import mediation  # noqa: F401


def test_share_heading_does_not_repeat_the_rationale():
    current = candidate()
    assert interaction_view(current).summary == "You created a skill that could help your team"
    view = advice_view([recommendation(current)])
    assert view.summary == ""
    assert view.items[0].preamble
    assert "Share a useful skill" not in view.to_text()
    weekly = advice_view([recommendation(current, explanation="Used across several days.")])
    assert weekly.summary == "You created a skill that could help your team"


def test_packaging_failure_has_no_notification_settings():
    item = {"assessment": {"reference": {"kind": "notice", "user_requested": True,
                                             "notice_kind": "share_packaging_failed"}},
            "advice": {"title": "Share Packaging failed", "relevance": "recommend",
                       "explanation": "Nothing was uploaded or published."}}
    view = advice_view([item])
    assert view.summary == "Share Packaging failed"
    assert view.to_text().count("Share Packaging failed") == 1
    assert not view.actions and not view.items[0].actions
    ordinary = {"assessment": {"reference": {"kind": "notice"}},
                "advice": {"title": "Activity", "relevance": "digest", "explanation": "Team activity."}}
    assert advice_view([ordinary]).actions


def test_deferral_replay_cannot_queue_packaging(sharing):
    service, mediation, actor, shown, model, source, now = sharing
    service.client.display_org_id = "org"
    original = mediation.consent.resolve("org", shown["id"], actor, "defer")
    now[0] += 1
    replay = mediation.consent.resolve("org", shown["id"], actor, "confirm")
    assert original["deferred"] and replay["deferred"]
    assert replay["actions"] == []
    assert not any(j["reference"]["kind"] == "share_package" for j in mediation.queue.assessments("org"))
    with service.store.transaction() as db:
        assert db.execute("SELECT state FROM wisdom_consent WHERE id=?", (shown["id"],)).fetchone()[0] == "pending"
    model.assert_not_called()
    review = mediation.consent.resolve("org", shown["id"], actor, "review")
    assert review["deferred"]
    assert service.client.uploaded == 0


def test_native_deferral_retains_advice_without_status_or_live_controls(sharing, monkeypatch):
    from hermes_wisdom.mediation_view import resolve_surface_action
    service, mediation, actor, shown, *_ = sharing
    service.client.display_org_id = "org"
    monkeypatch.setattr("hermes_wisdom.mediation_view.WisdomConsent", lambda _: mediation.consent)
    for action in ("defer", "confirm", "checks.show", "review", "inspect.0"):
        view = resolve_surface_action(service, f"wi:agent:{action}:{shown['id']}",
                                      platform=actor.platform, actor_id=actor.actor_id, **actor.address)
        assert not view.actions and not view.navigation_actions
        assert all(not card.actions for card in view.items)
        assert "Deferred" not in view.to_text()
        assert "Useful team skill" in view.to_text()
        assert "This may help your team." in view.to_text()


def test_local_inspection_returns_without_authorizing_package():
    current = candidate(operation="publish")
    current["inspection"] = {"page": 0, "page_count": 1, "path": "SKILL.md", "content": "All content."}
    view = interaction_view(current)
    assert [a.label for a in view.actions] == ["Looks good."]
    assert view.actions[0].callback_data == "wi:agent:back:exact-package"
    current["inspection"]["page_count"] = 3
    view = interaction_view(current)
    assert [a.label for a in view.actions] == ["Next page", "Looks good."]
    assert not any("confirm" in (a.callback_data or "") for a in view.actions)


def test_install_and_update_lead_with_saved_inference_and_keep_material_warnings():
    from tests.wisdom.test_sharing_gallery import passing_facts
    for operation in ("install", "update"):
        current = candidate(operation=operation)
        current["facts"].update(passing_facts())
        current["facts"].update({"version": 2, "update_mode": None,
                                  "compatibility": {"outcome": "compatible"}})
        item = {"organization_name": "Example Team", "interaction": current,
                "assessment": {"reference": {"kind": "skill"}},
                "advice": {"title": "Team notes", "relevance": "recommend",
                           "explanation": "Records decisions and helps with your weekly meeting notes."}}
        direct = interaction_view(current)
        assert [a.label for a in direct.actions] == ["View Details", "Not Now", "Mute Skill Recommendations", "Install Skill"]
        view = advice_view([item])
        assert item["advice"]["explanation"] in view.items[0].detail
        assert "Would you like to install this skill?" in view.items[0].detail
        assert "Package facts" not in view.items[0].detail
        assert "Safe To Share" not in view.items[0].detail
        assert [a.label for a in view.items[0].actions] == ["View Details", "Not Now", "Mute Skill Recommendations", "Install Skill"]
        if operation == "install":
            assert "your organization (Example Team)" in view.summary
        expanded = advice_view([item], assessment_expanded=True)
        assert "Future updates: Organization default" in expanded.to_text() or operation == "update"
        current["facts"]["modified"] = True
        current["facts"]["compatibility"]["outcome"] = "blocked"
        current["actions"].remove("confirm")
        blocked = advice_view([item])
        assert "Local changes" in blocked.to_text() and "Compatibility: blocked" in blocked.to_text()
        assert not any(a.primary for a in blocked.items[0].actions)
        item.pop("organization_name")
        assert "Example Team" not in advice_view([item]).summary


def test_recipient_producer_routes_real_prompt_context_and_verified_org(mediation, monkeypatch):
    import json
    from types import SimpleNamespace
    from tests.wisdom.test_feed_mediation import enqueue
    from hermes_wisdom.mediation import assess
    instance, actor, *_ = mediation
    instance.service.organization_display_name.return_value = "Verified Team"
    monkeypatch.setattr(instance.consent, "present", lambda *a, **k: candidate(operation="install"))
    enqueue(instance)
    explanation = "Synthetic model fixture: records decisions for recurring meetings."
    def call(**kw):
        prompt = kw["messages"][0]["content"]
        assert "what the skill does" in prompt
        assert "routine passing checks" in prompt
        data = json.loads(kw["messages"][1]["content"])
        assert data["conversation_excerpts"][0]["content"] == "I organize weekly meetings."
        assert kw["tools"] == [] and kw["temperature"] == 0
        payload = {"advice": [{"assessment_id": data["untrusted_evidence"][0]["assessment_id"],
                              "title": "Meeting notes", "relevance": "recommend", "explanation": explanation}]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload), tool_calls=[]))])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", call)
    result = instance.prepare("org", actor, runtime={"model": "fixture", "provider": "fixture"},
                              history=[{"role": "user", "content": "I organize weekly meetings."}], assessor=assess)
    assert result[0]["advice"]["explanation"] == explanation
    assert result[0]["organization_name"] == "Verified Team"
    assert "Verified Team" in advice_view(result).summary
