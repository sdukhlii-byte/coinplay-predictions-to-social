import sys

import pytest


class _Resp:
    def __init__(self, status=200, text="Olá"):
        self.status_code = status
        self.text = text
        self._text = text

    def json(self):
        return {"content": [{"type": "text", "text": self._text}]}


def _load(monkeypatch, **env):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "s")
    monkeypatch.setenv("SOURCE_CHAT_ID", "-100")
    monkeypatch.setenv("THREADS_ENABLED", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    for k in ("TRANSLATE_TO", "ANTHROPIC_API_KEY", "TRANSLATE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for m in ("config", "translate"):
        sys.modules.pop(m, None)
    import translate
    return translate


def test_disabled_returns_text_unchanged(monkeypatch):
    tr = _load(monkeypatch)
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: pytest.fail("не должно вызываться"))
    assert tr.maybe("Link in bio") == "Link in bio"


def test_translate_requires_key(monkeypatch):
    with pytest.raises(RuntimeError):
        _load(monkeypatch, TRANSLATE_TO="pt-BR")


def test_translates_and_caches(monkeypatch):
    tr = _load(monkeypatch, TRANSLATE_TO="pt-BR", ANTHROPIC_API_KEY="k")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        return _Resp(text="Link na bio")

    monkeypatch.setattr(tr.requests, "post", fake_post)
    assert tr.maybe("Link in bio") == "Link na bio"
    assert tr.maybe("Link in bio") == "Link na bio"
    assert len(calls) == 1
    assert "Brazilian Portuguese" in calls[0]["system"]
    assert calls[0]["messages"][0]["content"] == "Link in bio"


def test_api_error_raises(monkeypatch):
    tr = _load(monkeypatch, TRANSLATE_TO="pt-BR", ANTHROPIC_API_KEY="k")
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: _Resp(status=529, text="overloaded"))
    with pytest.raises(tr.TranslateError):
        tr.maybe("Some post")


def test_runaway_output_rejected(monkeypatch):
    tr = _load(monkeypatch, TRANSLATE_TO="pt-BR", ANTHROPIC_API_KEY="k")
    monkeypatch.setattr(tr.requests, "post", lambda *a, **k: _Resp(text="x" * 5000))
    with pytest.raises(tr.TranslateError):
        tr.maybe("Short post")
