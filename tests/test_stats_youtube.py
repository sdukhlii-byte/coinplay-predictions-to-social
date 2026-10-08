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


def test_instagram_insights_failure_is_reported_not_zero(env, monkeypatch):
    db = env
    _burst(db, "i", "posted", {"instagram": ["m1"]})
    import stats
    monkeypatch.setattr(stats, "_account_usernames", lambda: {})
    monkeypatch.setattr(stats.instagram_api, "get_insights",
                        lambda mid: {"views": 0, "likes": 3, "url": None,
                                     "error": "(#10) permission denied"})
    data = stats.collect(1)
    ig = data["platforms"]["instagram"]
    assert ig["errors"] == 1 and "permission" in ig["last_error"]
    text = stats.format_text(data)
    assert "без данных" in text and "причина" in text


def test_youtube_error_reason_shown_in_report(env, monkeypatch):
    db = env
    _burst(db, "y", "posted", {"youtube": ["v1"]})
    import stats

    def boom(ids):
        raise RuntimeError("videos.list 403: нет права youtube.readonly")

    monkeypatch.setattr(stats, "_account_usernames", lambda: {})
    monkeypatch.setattr(stats.youtube_api, "get_metrics", boom)
    text = stats.format_text(stats.collect(1))
    assert "youtube.readonly" in text


def test_video_kit_does_not_collide_with_image_kit_dedupe(env):
    import importlib, sys
    sys.modules.pop("worker", None)
    import worker
    b1 = {"id": "img1", "manifest": "zipkit · social kit: A vs B", "chat_id": "-1"}
    b2 = {"id": "vid1", "manifest": "zipkit · A vs B", "chat_id": "-2"}
    txt = "A vs B\nLeague\n\nAI models pick A"
    ok1, _ = worker._claim(b1, "instagram", txt, [{"kind": "image"}])
    ok2, _ = worker._claim(b2, "instagram", txt, [{"kind": "video"}])
    ok3, _ = worker._claim({"id": "img2", "manifest": "zipkit · social kit: A vs B", "chat_id": "-3"},
                           "instagram", txt, [{"kind": "image"}])
    assert ok1 and ok2 and not ok3


def test_requeue_reopens_platforms_closed_as_duplicates(env):
    db = env
    _burst(db, "r", "posted", {"instagram": [], "youtube": ["v"]})
    assert db.requeue_burst("r")
    assert json.loads(db.get_burst("r")["results"]) == {"youtube": ["v"]}
