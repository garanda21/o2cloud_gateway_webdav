from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from o2gateway.o2.api import O2CloudApiClient
from o2gateway.o2.session import O2Cookie, O2Session, O2SessionStore, normalize_oauth_bundle
from o2gateway.settings import Settings


logger = logging.getLogger(__name__)


VALIDATION_RE = re.compile(r"validationkey[^A-Za-z0-9._-]+([A-Za-z0-9._-]{8,})", re.I)

CAPTURE_SCRIPT = """
(() => {
  const readStorage = (storage) => {
    const out = {};
    try {
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i);
        out[key] = storage.getItem(key);
      }
    } catch (_) {}
    return out;
  };
  const resources = [];
  try {
    for (const entry of performance.getEntriesByType('resource')) {
      if (entry && entry.name) resources.push(entry.name);
    }
  } catch (_) {}
  const payload = {
    url: location.href,
    cookie: document.cookie || '',
    userAgent: navigator.userAgent || '',
    localStorage: readStorage(localStorage),
    sessionStorage: readStorage(sessionStorage),
    seenUrls: Array.isArray(window.__o2GatewaySeenUrls) ? window.__o2GatewaySeenUrls.slice(-200) : [],
    resources: resources.slice(-200)
  };
  const text = JSON.stringify(payload);
  const match = text.match(/validationkey[^A-Za-z0-9._-]+([A-Za-z0-9._-]{8,})/i);
  payload.validationKey = match ? match[1] : '';
  return payload;
})();
"""

BOOTSTRAP_SCRIPT = """
(() => {
  if (window.__o2GatewayLoginPatchInstalled) return;
  window.__o2GatewayLoginPatchInstalled = true;
  window.__o2GatewaySeenUrls = window.__o2GatewaySeenUrls || [];
  const remember = (value) => {
    try {
      const url = typeof value === 'string' ? value : value && value.url ? value.url : '';
      if (url && !window.__o2GatewaySeenUrls.includes(url)) {
        window.__o2GatewaySeenUrls.push(url);
        if (window.__o2GatewaySeenUrls.length > 400) window.__o2GatewaySeenUrls.shift();
      }
    } catch (_) {}
  };
  const originalFetch = window.fetch;
  if (originalFetch) {
    window.fetch = function(input, init) {
      remember(input);
      return originalFetch.apply(this, arguments);
    };
  }
  try {
    const originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
      remember(url);
      return originalOpen.apply(this, arguments);
    };
  } catch (_) {}
})();
"""


class O2PlaywrightLoginService:
    def __init__(self, settings: Settings, session_store: O2SessionStore, api: O2CloudApiClient) -> None:
        self.settings = settings
        self.session_store = session_store
        self.api = api
        self._browser_lock = asyncio.Lock()

    async def login(self, timeout_seconds: int = 300) -> O2Session:
        async with self._browser_lock:
            return await self._login(timeout_seconds, headless=self.settings.o2_playwright_headless, silent=False)

    async def silent_reauthenticate(self, timeout_seconds: int = 45) -> bool:
        if not self.has_persistent_profile():
            return False
        async with self._browser_lock:
            try:
                await self._login(timeout_seconds, headless=True, silent=True)
                return True
            except Exception as ex:
                logger.warning(
                    "silent browser reauthentication unavailable",
                    extra={"provider": self.settings.cloud_provider, "errorType": type(ex).__name__},
                )
                return False

    async def keep_session_alive(self) -> bool:
        session = self.session_store.read()
        if session is None or not session.is_authenticated:
            return False
        try:
            await self.api.keepalive()
            logger.info("api session keepalive completed", extra={"provider": self.settings.cloud_provider})
            return True
        except Exception as ex:
            logger.warning(
                "api session keepalive failed",
                extra={"provider": self.settings.cloud_provider, "errorType": type(ex).__name__},
            )
            return False

    def has_persistent_profile(self) -> bool:
        profile = self._profile_dir()
        return profile.exists() and any(profile.iterdir())

    async def _login(self, timeout_seconds: int, *, headless: bool, silent: bool) -> O2Session:
        try:
            from playwright.async_api import async_playwright
        except Exception as ex:
            raise RuntimeError("Playwright is not installed. Install with pip install '.[login]' and playwright install chromium.") from ex

        if not headless and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Interactive O2 login requires a visible browser display. "
                "In Docker, configure an X11/VNC/noVNC display or use manual session import."
            )

        user_data_dir = self._profile_dir()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        seen_urls: list[str] = []
        network_state = O2BrowserSessionState()
        capture_tasks: set[asyncio.Task] = set()

        async with async_playwright() as p:
            context = None
            try:
                context = await p.chromium.launch_persistent_context(
                    str(user_data_dir),
                    headless=headless,
                    viewport={"width": 1280, "height": 800},
                )
                await context.add_init_script(BOOTSTRAP_SCRIPT)
                page = context.pages[0] if context.pages else await context.new_page()
                page.on("request", lambda request: self._on_request(request, seen_urls, network_state, capture_tasks))
                page.on("response", lambda response: self._on_response(response, network_state, capture_tasks))
                page.on("framenavigated", lambda frame: _remember(seen_urls, frame.url))
                await page.goto(self.settings.o2_login_url)
                deadline = asyncio.get_event_loop().time() + timeout_seconds
                while asyncio.get_event_loop().time() < deadline:
                    await _drain_tasks(capture_tasks)
                    session = await self._capture(context, page, seen_urls, network_state)
                    if session and await self.api.validate_session(session):
                        if not session.can_refresh and not silent:
                            logger.warning(
                                "interactive login completed without renewable oauth credentials",
                                extra={"provider": self.settings.cloud_provider},
                            )
                        self.session_store.save(session)
                        if silent:
                            logger.info("browser session silently renewed", extra={"provider": self.settings.cloud_provider})
                        return session
                    await asyncio.sleep(2)
                raise TimeoutError("O2 login timed out before a valid session was detected")
            finally:
                await _drain_tasks(capture_tasks)
                if context is not None:
                    await context.close()

    async def _capture(self, context, page, seen_urls: list[str], network_state: "O2BrowserSessionState") -> Optional[O2Session]:
        try:
            state = await page.evaluate(CAPTURE_SCRIPT)
        except Exception:
            state = {}
        serialized = json.dumps({"state": state, "seenUrls": seen_urls}, ensure_ascii=False)
        validation_key = (
            network_state.validation_key
            or (state or {}).get("validationKey")
            or _extract_validation_key(serialized)
            or _extract_validation_key("\n".join(seen_urls))
        )
        if not validation_key:
            return None
        cookies = []
        login_origin = _origin_for(self.settings.o2_login_url or self.settings.o2_api_base_url)
        login_host = urlparse(login_origin).hostname or ""
        for cookie in await context.cookies(login_origin):
            domain = cookie.get("domain") or login_host
            if _cookie_matches_host(domain, login_host) and cookie.get("name") and cookie.get("value"):
                cookies.append(O2Cookie(cookie["name"], cookie["value"], domain, cookie.get("path") or "/"))
        return O2Session(
            validation_key=validation_key,
            cookies=cookies,
            user_agent=(state or {}).get("userAgent") or "",
            oauth_bundle=network_state.oauth_bundle,
            device_id=network_state.device_id,
            device_name=network_state.device_name,
            encryption_token=network_state.encryption_token,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def clear_session_cache(self) -> None:
        shutil.rmtree(self._profile_dir(), ignore_errors=True)

    def _profile_dir(self) -> Path:
        provider = re.sub(r"[^a-z0-9_.-]+", "-", self.settings.cloud_provider.lower()).strip("-") or "cloud"
        return Path(self.settings.config_dir) / "playwright-o2-profile" / provider

    def _on_request(self, request, seen_urls: list[str], network_state: "O2BrowserSessionState", tasks: set[asyncio.Task]) -> None:
        _remember(seen_urls, request.url)
        if _is_oauth_login_url(request.url):
            _schedule(_capture_oauth_request(request, network_state), tasks)

    def _on_response(self, response, network_state: "O2BrowserSessionState", tasks: set[asyncio.Task]) -> None:
        if _is_oauth_login_url(response.url):
            _schedule(_capture_oauth_response(response, network_state), tasks)


def _extract_validation_key(text: str) -> Optional[str]:
    match = VALIDATION_RE.search(text or "")
    return match.group(1) if match else None


def _remember(seen_urls: list[str], url: str) -> None:
    if url and url not in seen_urls:
        seen_urls.append(url)
        del seen_urls[:-400]


@dataclass
class O2BrowserSessionState:
    validation_key: str = ""
    oauth_bundle: str = ""
    device_id: str = ""
    device_name: str = ""
    encryption_token: str = ""


async def _capture_oauth_request(request, state: O2BrowserSessionState) -> None:
    try:
        headers = await request.all_headers()
    except Exception:
        return
    oauth_bundle = normalize_oauth_bundle(headers.get("authorization", ""))
    if oauth_bundle:
        state.oauth_bundle = oauth_bundle
    state.device_id = headers.get("x-deviceid") or state.device_id
    state.device_name = headers.get("x-devicename") or state.device_name


async def _capture_oauth_response(response, state: O2BrowserSessionState) -> None:
    try:
        headers = await response.all_headers()
    except Exception:
        headers = {}
    oauth_bundle = normalize_oauth_bundle(headers.get("authorization", ""))
    if oauth_bundle:
        state.oauth_bundle = oauth_bundle
    try:
        payload = await response.json()
    except Exception:
        return
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return
    state.oauth_bundle = normalize_oauth_bundle(str(data.get("access_token") or "")) or state.oauth_bundle
    state.validation_key = str(data.get("validationkey") or state.validation_key)
    state.encryption_token = str(data.get("encryption-token") or data.get("encryptionToken") or state.encryption_token)


def _is_oauth_login_url(raw_url: str) -> bool:
    try:
        return urlparse(raw_url).path.rstrip("/").endswith("/login/oauth")
    except Exception:
        return False


def _schedule(coro, tasks: set[asyncio.Task]) -> None:
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _drain_tasks(tasks: set[asyncio.Task]) -> None:
    if tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)


def _origin_for(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or raw_url.strip("/").split("/")[0]
    return f"{scheme}://{host}/"


def _cookie_matches_host(domain: str, host: str) -> bool:
    clean = domain.lstrip(".").lower()
    host = host.lower()
    return bool(clean and host and (host == clean or host.endswith("." + clean)))


@dataclass
class O2LoginJobState:
    state: str = "idle"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None


class O2LoginCoordinator:
    def __init__(self, settings: Settings, session_store: O2SessionStore, login_service: Optional[O2PlaywrightLoginService]) -> None:
        self.settings = settings
        self.session_store = session_store
        self.login_service = login_service
        self._task: Optional[asyncio.Task] = None
        self._state = O2LoginJobState()

    async def start(self) -> dict[str, object]:
        if self.login_service is None:
            raise RuntimeError("Playwright login is not available")
        if self._task and not self._task.done():
            return self.status()
        now = datetime.now(timezone.utc).isoformat()
        self._state = O2LoginJobState(state="running", started_at=now)
        self._task = asyncio.create_task(self._run(), name="o2-login")
        return self.status()

    def status(self) -> dict[str, object]:
        session = self.session_store.read()
        return {
            "state": self._state.state,
            "startedAt": self._state.started_at,
            "finishedAt": self._state.finished_at,
            "error": self._state.error,
            "configured": bool(session and session.is_authenticated),
            "novncUrl": self.settings.novnc_url(),
        }

    def reset(self) -> None:
        self._state = O2LoginJobState()

    async def _run(self) -> None:
        assert self.login_service is not None
        try:
            await self.login_service.login()
            self._state.state = "succeeded"
            self._state.error = None
        except Exception as ex:
            self._state.state = "failed"
            self._state.error = str(ex)
        finally:
            self._state.finished_at = datetime.now(timezone.utc).isoformat()
