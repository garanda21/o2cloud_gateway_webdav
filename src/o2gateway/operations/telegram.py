from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from o2gateway.settings import Settings, read_secret


logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None) -> None:
        self.settings = settings
        self._bot_token = read_secret(settings.telegram_bot_token, settings.telegram_bot_token_file)
        self._chat_id = (settings.telegram_chat_id or "").strip()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._last_expiry_marker: Optional[str] = None
        self._last_attempt_monotonic: Optional[float] = None
        self.last_notification_at: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def notify_session_expired(self, expired_at: str) -> bool:
        if not self.configured or self._last_expiry_marker == expired_at:
            return False
        now = time.monotonic()
        if self._last_attempt_monotonic is not None:
            elapsed = now - self._last_attempt_monotonic
            if elapsed < max(5, self.settings.telegram_alert_retry_seconds):
                return False
        self._last_attempt_monotonic = now
        message = (
            f"⚠️ Sesión de {self.settings.provider_label()} caducada\n\n"
            "El gateway ya no puede acceder a tus archivos. Inicia sesión de nuevo desde el panel de administración.\n\n"
            f"Panel: {self.settings.app_base_url.rstrip('/')}{self.settings.normalized_admin_base()}\n"
            f"Detectado: {expired_at}"
        )
        if not await self._send(message):
            return False
        self._last_expiry_marker = expired_at
        self._last_attempt_monotonic = None
        return True

    async def send_test(self) -> bool:
        if not self.configured:
            self.last_error = "Telegram no está configurado"
            return False
        message = (
            "✅ Notificaciones activas\n\n"
            f"{self.settings.provider_label()} WebDAV Gateway puede enviarte alertas a este chat."
        )
        return await self._send(message)

    def status(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "lastNotificationAt": self.last_notification_at,
            "lastError": self.last_error,
        }

    async def _send(self, text: str) -> bool:
        if not self.configured:
            return False
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            response = await self._client.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "link_preview_options": {"is_disabled": True},
                },
            )
            if response.status_code >= 400:
                self.last_error = f"Telegram respondió HTTP {response.status_code}"
                logger.warning("telegram notification rejected", extra={"httpStatus": response.status_code})
                return False
        except httpx.TimeoutException:
            self.last_error = "Timeout contactando Telegram"
            logger.warning("telegram notification timed out")
            return False
        except httpx.HTTPError as ex:
            self.last_error = f"Error de red de Telegram ({type(ex).__name__})"
            logger.warning("telegram notification failed", extra={"errorType": type(ex).__name__})
            return False
        self.last_error = None
        self.last_notification_at = datetime.now(timezone.utc).isoformat()
        logger.info("telegram notification sent")
        return True
