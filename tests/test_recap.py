import importlib
import sys
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "s")
    monkeypatch.setenv("SOURCE_CHAT_ID", "-100")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    monkeypatch.setenv("THREADS_ENABLED", "false")
    for m in ("config", "recap", "db"):
        sys.modules.pop(m, None)
    yield
    for m in ("config", "recap", "db"):
        sys.modules.pop(m, None)


def _recap(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import recap
    return recap


def test_tally_ignores_ungraded_coinplay_pick(monkeypatch):
    r = _recap(monkeypatch)
    grades = [{"coinplay_ai_correct": True}, {"coinplay_ai_correct": False},
              {"coinplay_ai_correct": None}, {"coinplay_ai_correct": True}]
    assert r.tally(grades) == (2, 3)


def test_daily_text_percent_only_above_threshold(monkeypatch):
    r = _recap(monkeypatch)
    # реальные накопленные цифры из API: футбол 28/50, теннис 4/6
    totals = {"soccer": (28, 50), "tennis": (4, 6)}
    text = r.build_daily({"soccer": (3, 5), "tennis": (1, 1), "ufc": (0, 0)}, totals)
    assert "⚽ Fútbol: 3/5 aciertos · acumulado 56% (28/50)" in text
    # у тенниса выборка 6 < 20 — процента нет
    assert "🎾 Tenis: 1/1 aciertos" in text and "acumulado 67%" not in text
    # пустая вертикаль не попадает в пост
    assert "UFC" not in text


def test_daily_empty_when_nothing_graded(monkeypatch):
    r = _recap(monkeypatch)
    assert r.build_daily({"soccer": (0, 0)}, {}) == ""


def test_x_variant_fits_280_for_all_disciplines(monkeypatch):
    r = _recap(monkeypatch)
    per = {"soccer": (3, 5), "ufc": (2, 2), "tennis": (1, 1), "dota2": (4, 6), "cs2": (2, 3)}
    totals = {"soccer": (28, 50), "ufc": (13, 21), "dota2": (35, 49), "cs2": (12, 18)}
    assert len(r.build_daily(per, totals, with_total=False)) <= 280


def test_weekly_percent_threshold(monkeypatch):
    r = _recap(monkeypatch)
    text = r.build_weekly({"soccer": (12, 20), "ufc": (3, 4)}, {"soccer": (28, 50)})
    assert "⚽ Fútbol: 12/20 (60%) · acumulado 56%" in text
    assert "🥊 UFC: 3/4" in text and "(75%)" not in text


def test_day_window_madrid(monkeypatch):
    r = _recap(monkeypatch)
    now = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)  # 09:00 в Мадриде (CET, UTC+1)
    start, end = r.day_window(now)
    assert end.astimezone(timezone.utc) == datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc)
    assert start.astimezone(timezone.utc) == datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)


def test_fetch_grades_filters_window_and_dedupes(monkeypatch):
    r = _recap(monkeypatch)
    rows = [
        {"id": "a", "data": {"match_id": "m1", "graded_at": "2026-10-27T21:27:50+00:00", "coinplay_ai_correct": True}},
        {"id": "b", "data": {"match_id": "m1", "graded_at": "2026-10-27T21:27:50+00:00", "coinplay_ai_correct": True}},
        {"id": "c", "data": {"match_id": "m2", "graded_at": "2026-10-19T21:27:50+00:00", "coinplay_ai_correct": False}},
    ]
    monkeypatch.setattr(r, "_get_all", lambda path, params: rows)
    start = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc)
    got = r.fetch_grades("soccer", start, end)
    assert [g["match_id"] for g in got] == ["m1"]


def test_fetch_totals_takes_only_coinplay_ai(monkeypatch):
    r = _recap(monkeypatch)
    rows = [
        {"discipline": "soccer", "data": {"model": "consensus", "correct": 43, "total": 71, "title": "soccer"}},
        {"discipline": "soccer", "data": {"model": "coinplay_ai", "correct": 28, "total": 50, "title": "soccer"}},
    ]
    monkeypatch.setattr(r, "_get_all", lambda path, params: rows)
    assert r.fetch_totals() == {"soccer": (28, 50)}


def test_cert_pin_mismatch_blocks_before_sending_key(monkeypatch):
    r = _recap(monkeypatch, COINPLAY_API_CERT_SHA256="AA:BB", COINPLAY_API_KEY="k")

    class Sock:
        def getpeercert(self, binary_form=False):
            return b"cert"

    class Conn:
        sock = Sock()
        sent = False

        def connect(self): pass
        def close(self): pass
        def request(self, *a, **k): Conn.sent = True

    monkeypatch.setattr(r.http.client, "HTTPSConnection", lambda *a, **k: Conn())
    with pytest.raises(r.RecapError, match="Отпечаток"):
        r._get("catalog")
    assert Conn.sent is False


def test_recap_goes_to_threads_only_by_default(monkeypatch):
    r = _recap(monkeypatch, THREADS_ENABLED="true", THREADS_ACCESS_TOKEN="t",
               THREADS_USER_ID="1", X_ENABLED="true", X_API_KEY="a", X_API_SECRET="b",
               X_ACCESS_TOKEN="c", X_ACCESS_SECRET="d")
    assert r._platforms() == ["threads"]


def test_x_only_service_gets_no_recap(monkeypatch):
    r = _recap(monkeypatch, X_ENABLED="true", X_API_KEY="a", X_API_SECRET="b",
               X_ACCESS_TOKEN="c", X_ACCESS_SECRET="d")
    assert r._platforms() == []


def test_x_can_be_enabled_explicitly(monkeypatch):
    r = _recap(monkeypatch, RECAP_PLATFORMS="threads,x", X_ENABLED="true", X_API_KEY="a",
               X_API_SECRET="b", X_ACCESS_TOKEN="c", X_ACCESS_SECRET="d")
    assert r._platforms() == ["x"]
