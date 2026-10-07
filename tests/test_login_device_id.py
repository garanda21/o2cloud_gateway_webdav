import asyncio
from types import SimpleNamespace

import pytest

from o2gateway.o2.login import (
    O2BrowserSessionState,
    O2PlaywrightLoginService,
    _capture_api_request,
    _is_api_url,
)
from o2gateway.settings import MOVISTAR_API_BASE_URL, O2_API_BASE_URL

DEVICE_HEADERS = {"x-deviceid": "browser-device", "x-devicename": "Chrome"}


def _request(headers, url="https://cloud.o2online.es/sapi/profile/role?validationkey=x"):
    async def all_headers():
        return headers

    return SimpleNamespace(url=url, all_headers=all_headers)


def _service(provider, api_base_url):
    settings = SimpleNamespace(cloud_provider=provider, o2_api_base_url=api_base_url)
    return O2PlaywrightLoginService(settings, session_store=None, api=None)


async def _observe(service, request):
    state = O2BrowserSessionState()
    tasks = set()
    service._on_request(request, [], state, tasks)
    if tasks:
        await asyncio.gather(*tasks)
    return state


async def test_web_device_id_is_taken_from_api_requests():
    state = O2BrowserSessionState()

    await _capture_api_request(_request({"x-deviceid": "browser-device", "x-devicename": "Chrome"}), state)

    assert state.device_id == "browser-device"
    assert state.device_name == "Chrome"


async def test_a_known_device_id_is_not_overwritten():
    state = O2BrowserSessionState(device_id="oauth-device", device_name="oauth-name")

    await _capture_api_request(_request({"x-deviceid": "other-device", "x-devicename": "other"}), state)

    assert state.device_id == "oauth-device"
    assert state.device_name == "oauth-name"


async def test_requests_without_device_headers_change_nothing():
    state = O2BrowserSessionState()

    await _capture_api_request(_request({"accept": "*/*"}), state)

    assert state.device_id == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://cloud.o2online.es/sapi/profile/role?validationkey=x",
        "https://cloud.o2online.es/sapi/media/folder/root",
    ],
)
def test_requests_to_the_configured_api_origin_are_inspected(url):
    assert _is_api_url(url, O2_API_BASE_URL)


@pytest.mark.parametrize(
    "url",
    [
        "https://cloud.o2online.es/ui/app/src/portal-es.json?validationkey=x",
        "https://evil.example/sapi/profile/role",
        "https://upload.cloud.o2online.es/sapi/upload?action=save",
        "https://cloud.o2online.es.evil.example/sapi/profile",
        "http://cloud.o2online.es/sapi/profile",
        "https://cloud.o2online.es:8443/sapi/profile",
        "https://micloud.movistar.es/sapi/profile",
    ],
)
def test_requests_outside_the_configured_api_origin_are_ignored(url):
    assert not _is_api_url(url, O2_API_BASE_URL)


async def test_o2_captures_the_device_id_from_its_api_requests():
    state = await _observe(_service("o2", O2_API_BASE_URL), _request(DEVICE_HEADERS))

    assert state.device_id == "browser-device"


async def test_o2_ignores_device_ids_sent_to_other_hosts():
    request = _request(DEVICE_HEADERS, url="https://evil.example/sapi/profile")

    state = await _observe(_service("o2", O2_API_BASE_URL), request)

    assert state.device_id == ""


async def test_movistar_keeps_its_previous_behavior():
    request = _request(DEVICE_HEADERS, url="https://micloud.movistar.es/sapi/profile")

    state = await _observe(_service("movistar", MOVISTAR_API_BASE_URL), request)

    assert state.device_id == ""
