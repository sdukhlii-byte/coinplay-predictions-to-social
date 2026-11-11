"""current_token(): токен из переменной окружения побеждает устаревший токен
в БД, если переменную заменили (инцидент 11.11.2026: 190/460 после смены
токена в Railway, т.к. БД перекрывала переменную)."""

import hashlib
import os
import sys
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
for _k, _v in {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_STRING_SESSION": "s",
               "SOURCE_CHAT_ID": "-100", "PUBLIC_BASE_URL": "https://x.example",
               "THREADS_ENABLED": "false"}.items():
    os.environ.setdefault(_k, _v)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import instagram_api  # noqa: E402


class FakeDB:
    def __init__(self):
        self.s = {}

    def get_state(self, key, default=None):
        return self.s.get(key, default)

    def set_state(self, key, value):
        self.s[key] = value


def _fp(tok):
    return hashlib.sha256(tok.encode()).hexdigest()[:16]


def test_env_token_used_when_db_empty(monkeypatch):
    monkeypatch.setattr(instagram_api, "db", FakeDB())
    monkeypatch.setattr(instagram_api, "INSTAGRAM_ACCESS_TOKEN", "ENV1")
    assert instagram_api.current_token() == "ENV1"


def test_stale_db_token_without_source_is_ignored(monkeypatch):
    """Состояние, записанное старой версией кода (без отпечатка), не должно
    перекрывать переменную окружения."""
    fake = FakeDB()
    fake.s["instagram_access_token"] = "OLD_INVALIDATED"
    monkeypatch.setattr(instagram_api, "db", fake)
    monkeypatch.setattr(instagram_api, "INSTAGRAM_ACCESS_TOKEN", "ENV_NEW")
    assert instagram_api.current_token() == "ENV_NEW"


def test_refreshed_db_token_used_while_env_unchanged(monkeypatch):
    fake = FakeDB()
    fake.s["instagram_access_token"] = "REFRESHED"
    fake.s["instagram_access_token_src"] = _fp("ENV1")
    monkeypatch.setattr(instagram_api, "db", fake)
    monkeypatch.setattr(instagram_api, "INSTAGRAM_ACCESS_TOKEN", "ENV1")
    assert instagram_api.current_token() == "REFRESHED"


def test_replacing_env_token_beats_refreshed_db_token(monkeypatch):
    fake = FakeDB()
    fake.s["instagram_access_token"] = "REFRESHED_FROM_ENV1"
    fake.s["instagram_access_token_src"] = _fp("ENV1")
    monkeypatch.setattr(instagram_api, "db", fake)
    monkeypatch.setattr(instagram_api, "INSTAGRAM_ACCESS_TOKEN", "ENV2")
    assert instagram_api.current_token() == "ENV2"


def test_refresh_stores_token_with_env_fingerprint(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(instagram_api, "db", fake)
    monkeypatch.setattr(instagram_api, "INSTAGRAM_ACCESS_TOKEN", "ENV1")
    monkeypatch.setattr(instagram_api, "META_APP_ID", "id")
    monkeypatch.setattr(instagram_api, "META_APP_SECRET", "secret")

    class R:
        status_code = 200
        text = ""
        def json(self):
            return {"access_token": "EXTENDED"}

    calls = []

    def fake_get(self_or_url=None, *a, **k):
        calls.append(1)
        return R()

    monkeypatch.setattr(instagram_api, "_get",
                        lambda path, params: {"data": {"expires_at": 1}})
    monkeypatch.setattr(instagram_api.requests, "get", fake_get)
    assert instagram_api.refresh_token_if_needed() is True
    assert instagram_api.current_token() == "EXTENDED"
    instagram_api.INSTAGRAM_ACCESS_TOKEN = "ENV2"
    assert instagram_api.current_token() == "ENV2"
