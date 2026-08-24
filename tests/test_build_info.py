from o2gateway.operations.build_info import get_build_info


def test_configured_build_info_is_normalized():
    info = get_build_info("v1.2.3", "abcdef1234567890")

    assert info.version == "1.2.3"
    assert info.commit == "abcdef123456"
    assert info.repository_url == "https://github.com/garanda21/o2cloud_gateway_webdav"


def test_unknown_commit_is_hidden():
    assert get_build_info("1.2.3", "unknown").commit is None
