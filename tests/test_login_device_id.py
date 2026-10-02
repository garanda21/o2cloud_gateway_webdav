from types import SimpleNamespace

from o2gateway.o2.login import O2BrowserSessionState, _capture_api_request, _is_api_url


def _request(headers):
    async def all_headers():
        return headers

    return SimpleNamespace(all_headers=all_headers)


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


def test_only_provider_api_paths_are_inspected():
    assert _is_api_url("https://cloud.o2online.es/sapi/profile/role?validationkey=x")
    assert not _is_api_url("https://cloud.o2online.es/ui/app/src/portal-es.json?validationkey=x")
