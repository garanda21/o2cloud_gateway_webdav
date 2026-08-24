import asyncio
import json

import httpx
import pytest

from o2gateway.o2.api import O2CloudApiClient
from o2gateway.o2.session import O2Session
from o2gateway.operations.logging import redact
from o2gateway.operations.telegram import TelegramNotifier
from o2gateway.settings import Settings


@pytest.mark.asyncio
async def test_expiry_notification_is_sent_once_per_expired_session():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        _env_file=None,
        cloud_provider="o2",
        app_base_url="https://gateway.example",
        telegram_bot_token="secret-token",
        telegram_bot_token_file=None,
        telegram_chat_id="123456",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(settings, client)

    assert await notifier.notify_session_expired("2026-08-22T12:00:00+00:00") is True
    assert await notifier.notify_session_expired("2026-08-22T12:00:00+00:00") is False
    assert await notifier.notify_session_expired("2026-08-22T13:00:00+00:00") is True

    assert len(requests) == 2
    assert requests[0].url.path == "/botsecret-token/sendMessage"
    payload = json.loads(requests[0].content)
    assert payload["chat_id"] == "123456"
    assert "Sesión de O2 Cloud caducada" in payload["text"]
    assert "https://gateway.example/admin" in payload["text"]
    assert notifier.status()["lastNotificationAt"] is not None
    await client.aclose()


@pytest.mark.asyncio
async def test_notifier_does_nothing_without_complete_configuration():
    settings = Settings(_env_file=None, telegram_bot_token="token", telegram_bot_token_file=None, telegram_chat_id=None)
    notifier = TelegramNotifier(settings)
    try:
        assert notifier.configured is False
        assert await notifier.notify_session_expired("2026-08-22T12:00:00+00:00") is False
        assert await notifier.send_test() is False
        assert notifier.status()["lastError"] == "Telegram no está configurado"
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_failed_notification_has_safe_error_and_is_throttled():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(401, json={"ok": False})

    settings = Settings(
        _env_file=None,
        telegram_bot_token="do-not-leak",
        telegram_bot_token_file=None,
        telegram_chat_id="99",
        telegram_alert_retry_seconds=0,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(settings, client)

    assert await notifier.notify_session_expired("expired-1") is False
    assert await notifier.notify_session_expired("expired-1") is False
    assert request_count == 1
    assert notifier.last_error == "Telegram respondió HTTP 401"
    assert "do-not-leak" not in notifier.last_error
    await client.aclose()


def test_telegram_bot_token_is_redacted_from_http_logs():
    value = redact("HTTP Request: POST https://api.telegram.org/bot123456:very-secret/sendMessage")

    assert value == "HTTP Request: POST https://api.telegram.org/bot***/sendMessage"


@pytest.mark.asyncio
async def test_expiry_marker_is_stable_until_the_session_changes():
    class SessionStore:
        session = O2Session(validation_key="first-session")

        def read(self):
            return self.session

    session_store = SessionStore()
    api = O2CloudApiClient(Settings(_env_file=None), session_store)
    try:
        api._mark_session_expired()
        first_marker = api.session_expired_at()
        api._mark_session_expired()

        assert api.session_expired_at() == first_marker
        session_store.session = O2Session(validation_key="renewed-session")
        assert api.session_expired_at() is None
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_session_expiry_event_is_emitted_immediately_once_per_session():
    class SessionStore:
        session = O2Session(validation_key="first-session")

        def read(self):
            return self.session

    session_store = SessionStore()
    api = O2CloudApiClient(Settings(_env_file=None), session_store)
    try:
        first_waiter = asyncio.create_task(api.wait_for_session_expiry())
        await asyncio.sleep(0)
        assert first_waiter.done() is False

        api._mark_session_expired()
        assert await asyncio.wait_for(first_waiter, timeout=0.1) == api.session_expired_at()

        duplicate_waiter = asyncio.create_task(api.wait_for_session_expiry())
        api._mark_session_expired()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(duplicate_waiter, timeout=0.01)

        session_store.session = O2Session(validation_key="renewed-session")
        assert api.session_expired_at() is None
        renewed_waiter = asyncio.create_task(api.wait_for_session_expiry())
        api._mark_session_expired()
        assert await asyncio.wait_for(renewed_waiter, timeout=0.1) == api.session_expired_at()
    finally:
        await api.close()
