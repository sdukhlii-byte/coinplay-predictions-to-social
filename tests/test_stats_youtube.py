import json
import sys
import time

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "s")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("config", "db", "stats", "youtube_api", "x_api", "threads_api", "instagram_api"):
        sys.modules.pop(m, None)
    import db
    db.init()
    return db


def _burst(db, bid, status, results):
    now = time.time()
    with db._lock:
        db._conn.execute(
            "INSERT INTO bursts (id, chat_id, status, results, created_at, last_msg_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (bid, "-1", status, json.dumps(results), now, now, now))
        db._conn.commit()


def test_posted_since_includes_failed_burst_with_published_ids(env):
    db = env
    _burst(db, "a", "posted", {"instagram": ["1"]})
    _burst(db, "b", "failed", {"instagram": ["2"], "youtube": ["vid"]})   # X упал
    _burst(db, "c", "failed", {})                                          # ничего не вышло
    ids = {b["id"] for b in db.posted_since(time.time() - 60)}
    assert ids == {"a", "b"}


def test_youtube_metrics_included_in_report(env, monkeypatch):
    db = env
    _burst(db, "b", "failed", {"youtube": ["vid1", "vid2"]})
    import stats
    monkeypatch.setattr(stats, "_account_usernames", lambda: {})
    monkeypatch.setattr(stats.youtube_api, "get_metrics",
                        lambda ids: {"vid1": {"views": 120, "likes": 7}})
    data = stats.collect(1)
    yt = data["platforms"]["youtube"]
    assert yt == {"posts": 2, "views": 120, "likes": 7, "errors": 1}
    assert any(p["url"] == "https://www.youtube.com/shorts/vid1" for p in data["posts"])
    assert "YouTube Shorts" in stats.format_text(data)
