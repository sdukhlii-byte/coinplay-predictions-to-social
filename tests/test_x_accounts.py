import importlib
import json
import os
import sys

import pytest

ACC = lambda n: {"api_key": f"k{n}", "api_secret": f"s{n}",
                 "access_token": f"t{n}", "access_secret": f"a{n}"}


def _reload_config(monkeypatch, **env):
    for k in ("X_ENABLED", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
              "X_ACCESS_SECRET", "X_ACCOUNTS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for m in ("config", "x_api"):
        sys.modules.pop(m, None)
    import config
    import x_api
    return config, x_api


@pytest.fixture(autouse=True)
def _base_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    yield
    for m in ("config", "x_api"):
        sys.modules.pop(m, None)


def test_per_source_account_with_default_fallback(monkeypatch):
    cfg, x = _reload_config(
        monkeypatch, X_ENABLED="true", X_API_KEY="dk", X_API_SECRET="ds",
        X_ACCESS_TOKEN="dt", X_ACCESS_SECRET="da",
        X_ACCOUNTS=json.dumps({"-100111": ACC(1)}),
    )
    assert x.account_for(-100111)["api_key"] == "k1"
    assert x.account_for("-100111")["api_key"] == "k1"
    assert x.account_for(-100999)["api_key"] == "dk"
    assert len(x.all_accounts()) == 2


def test_only_accounts_no_default_skips_unknown_source(monkeypatch):
    cfg, x = _reload_config(
        monkeypatch, X_ENABLED="true",
        X_ACCOUNTS=json.dumps({"-100111": ACC(1), "-100222": ACC(2)}),
    )
    assert x.account_for(-100222)["api_key"] == "k2"
    assert x.account_for(-100999) is None
    assert len(x.all_accounts()) == 2


def test_incomplete_account_rejected(monkeypatch):
    with pytest.raises(RuntimeError, match="X_ACCOUNTS"):
        _reload_config(monkeypatch, X_ENABLED="true",
                       X_ACCOUNTS=json.dumps({"-1": {"api_key": "x"}}))


def test_bad_json_rejected_without_leaking_value(monkeypatch):
    with pytest.raises(RuntimeError) as e:
        _reload_config(monkeypatch, X_ENABLED="true", X_ACCOUNTS="{secret-key")
    assert "secret-key" not in str(e.value)


def test_enabled_without_any_keys_fails(monkeypatch):
    with pytest.raises(RuntimeError):
        _reload_config(monkeypatch, X_ENABLED="true")


def test_auth_uses_given_account(monkeypatch):
    cfg, x = _reload_config(monkeypatch, X_ENABLED="true",
                            X_ACCOUNTS=json.dumps({"-1": ACC(7)}))
    assert x._auth(x.account_for(-1)).client.client_key == "k7"
