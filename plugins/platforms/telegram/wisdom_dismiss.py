"""Remove controls from the original Telegram card without replacing its copy."""
import re

from telegram.error import BadRequest
from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id


def without_controls(value):
    if isinstance(value, list):
        return [without_controls(item) for item in value
                if not isinstance(item, dict) or not any(key in item for key in ("callback_data", "buttons", "button"))]
    if isinstance(value, dict):
        return {key: without_controls(item) for key, item in value.items()
                if key not in {"callback_data", "buttons", "button", "reply_markup"}}
    return value


async def dismiss_original(adapter, query):
    message = getattr(query, "message", None)
    request = getattr(getattr(adapter, "_bot", None), "do_api_request", None)
    if message is None or not callable(request):
        return False
    rich = getattr(message, "rich_message", None)
    if rich is None:
        rich = (getattr(message, "api_kwargs", None) or {}).get("rich_message")
    mapped = adapter._wisdom_api_mapping(rich)
    payload = {"chat_id": normalize_telegram_chat_id(message.chat_id),
               "message_id": int(message.message_id), "reply_markup": {"inline_keyboard": []}}
    method = "editMessageReplyMarkup"
    if mapped:
        preserved = without_controls(mapped)
        if isinstance(mapped.get("html"), str):
            preserved["html"] = re.sub(
                r"<tg-button-row\b[^>]*>.*?</tg-button-row>|<tg-button\b[^>]*>.*?</tg-button>",
                "", mapped["html"], flags=re.DOTALL,
            )
        payload["rich_message"] = preserved
        method = "editMessageText"
    elif not getattr(message, "text", None):
        return False
    try:
        await request(method, api_kwargs=payload)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    return True
