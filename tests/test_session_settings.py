import pytest

from o2gateway.settings import Settings


@pytest.mark.parametrize("provider, expected", [("o2", 0), ("O2", 0), ("movistar", 21600)])
def test_proactive_renewal_defaults_follow_provider(monkeypatch, provider, expected):
    monkeypatch.delenv("O2_PROACTIVE_REAUTH_SECONDS", raising=False)
    settings = Settings(_env_file=None, cloud_provider=provider)
    assert settings.o2_proactive_reauth_seconds == expected
    assert settings.o2_session_keepalive_seconds == 300


@pytest.mark.parametrize("provider, interval", [("o2", 120), ("movistar", 0)])
def test_explicit_environment_interval_overrides_provider_default(monkeypatch, provider, interval):
    monkeypatch.setenv("O2_PROACTIVE_REAUTH_SECONDS", str(interval))
    settings = Settings(_env_file=None, cloud_provider=provider)
    assert settings.o2_proactive_reauth_seconds == interval
