"""Тесты публикации в YouTube (сеть замокана). Запуск из корня репозитория:
    python -m pytest tests -q
"""
import os
import sys

for k, v in {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_STRING_SESSION": "s",
             "SOURCE_CHAT_ID": "-100", "PUBLIC_BASE_URL": "https://x.example",
             "THREADS_ENABLED": "false", "YOUTUBE_ENABLED": "true",
             "YOUTUBE_CLIENT_ID": "cid", "YOUTUBE_CLIENT_SECRET": "sec",
             "YOUTUBE_REFRESH_TOKEN": "rt", "DATA_DIR": "/tmp/yt-test-data"}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import youtube_api  # noqa: E402


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers, self.text = status, data or {}, headers or {}, ""

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _reset():
    youtube_api._token.update(value="", exp=0.0)


def test_split_text_title_and_description():
    t, d = youtube_api.split_text("🤖 10 AI models <predict>: A vs B\n\nline1\nline2")
    assert t == "🤖 10 AI models predict: A vs B"
    assert "line1" in d and d.endswith("#Shorts")
    long_t, _ = youtube_api.split_text("x" * 300)
    assert len(long_t) == 100


def test_token_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(youtube_api.requests, "post",
                        lambda url, **kw: calls.append(url) or Resp(data={"access_token": "AT", "expires_in": 3600}))
    assert youtube_api.access_token() == "AT"
    assert youtube_api.access_token() == "AT"
    assert len(calls) == 1


def test_token_error_has_hint(monkeypatch):
    monkeypatch.setattr(youtube_api.requests, "post",
                        lambda url, **kw: Resp(400, {"error": "invalid_grant", "error_description": "expired"}))
    with pytest.raises(youtube_api.YouTubeError) as e:
        youtube_api.access_token()
    assert "invalid_grant" in str(e.value) and "youtube_auth" in str(e.value)


def test_publish_resumable_flow(monkeypatch, tmp_path):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x" * 1000)
    monkeypatch.setattr(youtube_api.db, "get_media", lambda key: {"path": str(vid)})
    seen = {}

    def post(url, **kw):
        if url == youtube_api.TOKEN_URL:
            return Resp(data={"access_token": "AT", "expires_in": 3600})
        seen["meta"] = kw["data"]
        seen["params"] = kw["params"]
        return Resp(headers={"Location": "https://upload.example/session"})

    def put(url, **kw):
        seen["put"] = url
        return Resp(data={"id": "VID123"})
    monkeypatch.setattr(youtube_api.requests, "post", post)
    monkeypatch.setattr(youtube_api.requests, "put", put)
    ids = youtube_api.publish("Title\n\nbody", [{"kind": "video", "key": "k"}])
    assert ids == ["VID123"] and seen["put"] == "https://upload.example/session"
    assert seen["params"]["uploadType"] == "resumable"
    assert b'"privacyStatus": "public"' in seen["meta"]


def test_publish_without_video_returns_empty():
    assert youtube_api.publish("t", [{"kind": "image", "key": "k"}]) == []


def test_upload_error_message(monkeypatch, tmp_path):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    monkeypatch.setattr(youtube_api.db, "get_media", lambda key: {"path": str(vid)})
    monkeypatch.setattr(youtube_api.requests, "post", lambda url, **kw: (
        Resp(data={"access_token": "AT", "expires_in": 3600}) if url == youtube_api.TOKEN_URL else
        Resp(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})))
    with pytest.raises(youtube_api.YouTubeError) as e:
        youtube_api.publish("t", [{"kind": "video", "key": "k"}])
    assert "quotaExceeded" in str(e.value)


def test_worker_routes_kit_to_youtube():
    import worker
    assert "youtube" in worker.FINALIZERS
    assert worker._finalize_youtube("t", [{"kind": "image"}]) is None
    import config
    assert "youtube" in config.ROUTING_DEFAULT["kit"]
