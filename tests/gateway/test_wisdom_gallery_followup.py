from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.gateway.test_telegram_wisdom_command import _adapter
from tests.wisdom.test_share_queue import sharing  # noqa: F401
from tests.wisdom.test_share_staging import staged  # noqa: F401


@pytest.mark.asyncio
async def test_not_now_silently_edits_original_and_replayed_confirm_cannot_share(sharing, monkeypatch):
    service, mediation, actor, shown, model, *_ = sharing
    service.client.display_org_id = "org"
    monkeypatch.setattr("hermes_wisdom.service.WisdomService", lambda: service)
    monkeypatch.setattr("hermes_wisdom.mediation_view.WisdomConsent", lambda _: mediation.consent)
    adapter = _adapter()
    adapter._is_callback_user_authorized = Mock(return_value=True)
    adapter._run_wisdom_profile_operation = AsyncMock(side_effect=lambda fn, **_: fn())
    query = SimpleNamespace(from_user=SimpleNamespace(id=actor.actor_id), answer=AsyncMock(),
                            message=SimpleNamespace(chat_id=42, message_id=19))
    for action in ("defer", "confirm"):
        await adapter._handle_wisdom_callback(query, f"wi:agent:{action}:{shown['id']}",
            query_chat_id=actor.chat_id, query_chat_type="private", query_thread_id=actor.thread_id,
            query_user_name="Owner")
    assert query.answer.call_args_list[0].kwargs.get("text", "") == ""
    edits = adapter._bot.do_api_request.call_args_list
    assert len(edits) == 2
    for edit in edits:
        assert edit.args[0] == "editMessageText"
        args = edit.kwargs["api_kwargs"]
        assert args["message_id"] == 19
        assert "Deferred" not in args["rich_message"]["html"]
        assert "tg-button" not in args["rich_message"]["html"]
    adapter._bot.send_message.assert_not_awaited()
    model.assert_not_called()


@pytest.mark.asyncio
async def test_dismiss_keeps_original_text_and_removes_every_control():
    from gateway.wisdom_command import WisdomView
    from tests.gateway.test_slack_wisdom import _adapter as slack_adapter
    view = WisdomView("Not replacement text")
    view._dismissed = True
    telegram = _adapter()
    query = SimpleNamespace(message=SimpleNamespace(chat_id=42, message_id=19,
        rich_message={"html": '<h3>Original</h3><p>Exact history.</p><tg-button-row><tg-button type="callback_data" data="x">Share</tg-button></tg-button-row>'}))
    await telegram._edit_wisdom_command_view(query, view)
    sent = telegram._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]["html"]
    assert sent == '<h3>Original</h3><p>Exact history.</p>'
    slack = slack_adapter()
    client = SimpleNamespace(chat_update=AsyncMock())
    slack._get_client = Mock(return_value=client)
    body = {"channel": {"id": "D1"}, "team": {"id": "T1"},
            "message": {"ts": "19", "text": "Original history", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Exact history."}},
                {"type": "actions", "elements": [{"type": "button"}]}]}}
    await slack._update_wisdom_interaction(body, view)
    sent = client.chat_update.call_args.kwargs
    assert sent["text"] == "Original history"
    assert sent["blocks"] == body["message"]["blocks"][:1]


@pytest.mark.asyncio
async def test_dismiss_structured_telegram_card_preserves_content_and_retry():
    from gateway.wisdom_command import WisdomView
    from telegram.error import BadRequest
    adapter = _adapter()
    view = WisdomView("Fallback", _dismissed=True)
    rich = {"blocks": [{"text": "Exact history"}, {"buttons": [{"text": "Share", "callback_data": "x"}]}]}
    query = SimpleNamespace(message=SimpleNamespace(chat_id=42, message_id=19, rich_message=rich))
    await adapter._edit_wisdom_command_view(query, view)
    sent = adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]["rich_message"]
    assert sent["blocks"][0] == rich["blocks"][0]
    assert "callback_data" not in str(sent)
    assert "callback_data" in str(rich)
    adapter._bot.do_api_request.side_effect = BadRequest("Message is not modified")
    await adapter._edit_wisdom_command_view(query, view)
