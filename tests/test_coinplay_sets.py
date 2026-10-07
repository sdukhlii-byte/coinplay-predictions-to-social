"""Тесты coinplay_sets.py и его интеграции в generate.py — без сети, без
реального sets.json (сервер недоступен из песочницы, см. docstring модуля)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coinplay_sets   # noqa: E402
import generate        # noqa: E402
import state            # noqa: E402


# --------------------------------------------------------- rows_from_predictions --

def test_rows_from_predictions_football_uses_real_score():
    preds = [
        {"model": "openai/gpt-5", "lab": "ChatGPT", "winner": "team_a",
         "confidence": 0.7, "score": {"team_a": 2, "team_b": 1}, "short_rationale": "form"},
        {"model": "anthropic/claude-sonnet-4.5", "lab": "Claude", "winner": "team_b",
         "confidence": 0.6, "score": {"team_a": 0, "team_b": 1}, "short_rationale": "injuries"},
    ]
    rows = coinplay_sets.rows_from_predictions(preds, "football")
    assert rows[0] == {"label": "ChatGPT", "icon": "chatgpt", "model": "openai/gpt-5",
                       "home": 2, "away": 1, "reason": "form"}
    assert rows[1]["home"] == 0 and rows[1]["away"] == 1


def test_rows_from_predictions_skips_abstained():
    preds = [{"model": "x", "lab": "Grok", "winner": None}]
    assert coinplay_sets.rows_from_predictions(preds, "football") == []


def test_rows_from_predictions_ufc_has_no_real_score_encodes_winner_as_1_0():
    preds = [
        {"model": "m1", "lab": "ChatGPT", "winner": "team_a", "score": None,
         "method": "KO/TKO", "short_rationale": "power"},
        {"model": "m2", "lab": "Claude", "winner": "team_b", "score": None,
         "method": "Decision", "short_rationale": "grappling"},
    ]
    rows = coinplay_sets.rows_from_predictions(preds, "ufc")
    assert (rows[0]["home"], rows[0]["away"]) == (1, 0)
    assert (rows[1]["home"], rows[1]["away"]) == (0, 1)
    assert rows[0]["reason"].startswith("KO/TKO")


def test_icon_for_lab_known_and_unknown():
    assert coinplay_sets._icon_for_lab("ChatGPT") == "chatgpt"
    assert coinplay_sets._icon_for_lab("Grok") == "grok"
    assert coinplay_sets._icon_for_lab("SomeNewLab") == "somenewlab"


# --------------------------------------------------------------- match_from_set --

def _set(vertical="football", status="ungraded", start_at="2026-12-01T18:00:00+00:00",
        series_format=None, score_a=2, score_b=1):
    return {
        "match_id": "abc123", "vertical": vertical, "status": status,
        "match": {"team_a": "Team A", "team_b": "Team B", "event": "Big Cup",
                  "start_at": start_at, "series_format": series_format},
        "consensus": {"winner": "team_a", "votes": {"team_a": 3, "team_b": 1}},
        "predictions": [
            {"model": "m1", "lab": "ChatGPT", "winner": "team_a",
             "score": {"team_a": score_a, "team_b": score_b}},
            {"model": "m2", "lab": "Claude", "winner": "team_a",
             "score": {"team_a": score_a, "team_b": score_b}},
        ],
    }


def test_match_from_set_builds_expected_fields():
    m = coinplay_sets.match_from_set(_set())
    assert m["id"] == "coinplay-abc123"
    assert m["home"] == "Team A" and m["away"] == "Team B"
    assert m["date"] == "2026-12-01"
    assert m["competition"] == "Big Cup"
    assert m["vertical"] == "football"
    assert len(m["rows"]) == 2
    assert m["consensus"] == "TEAM A"
    assert m["consensus_note"] == "3 of 4 models agree"
    assert m["consensus_pct"] == 75


def test_match_from_set_appends_series_format_to_competition():
    m = coinplay_sets.match_from_set(_set(vertical="esports", series_format="Bo3"))
    assert m["competition"] == "Big Cup · Bo3"


def test_match_from_set_none_without_team_names():
    s = _set()
    s["match"]["team_a"] = ""
    assert coinplay_sets.match_from_set(s) is None


def test_match_from_set_none_when_all_abstained():
    s = _set()
    for p in s["predictions"]:
        p["winner"] = None
    assert coinplay_sets.match_from_set(s) is None


def test_match_from_set_tolerates_bad_start_at():
    s = _set(start_at="not-a-date")
    m = coinplay_sets.match_from_set(s)
    assert m["date"] == ""


# ------------------------------------------------------------------------ fetch --

def test_fetch_rejects_unknown_vertical():
    with pytest.raises(ValueError):
        coinplay_sets.fetch("tennis")


def test_fetch_raises_no_sets_found_when_vertical_absent(monkeypatch):
    monkeypatch.setattr(coinplay_sets, "fetch_raw", lambda: {"sets": [_set(vertical="ufc")]})
    with pytest.raises(coinplay_sets.NoSetsFound):
        coinplay_sets.fetch("football")


def test_fetch_skips_already_posted(monkeypatch):
    s1, s2 = _set(), _set()
    s1["match_id"], s2["match_id"] = "old", "new"
    monkeypatch.setattr(coinplay_sets, "fetch_raw", lambda: {"sets": [s1, s2]})
    monkeypatch.setattr(state, "already_posted", lambda keys: {"coinplay-old"} & keys)
    out = coinplay_sets.fetch("football", per_run=5)
    assert [m["id"] for m in out] == ["coinplay-new"]


def test_fetch_excludes_withdrawn(monkeypatch):
    s = _set(status="withdrawn")
    monkeypatch.setattr(coinplay_sets, "fetch_raw", lambda: {"sets": [s]})
    monkeypatch.setattr(state, "already_posted", lambda keys: set())
    with pytest.raises(coinplay_sets.NoSetsFound):
        coinplay_sets.fetch("football")


def test_fetch_respects_per_run(monkeypatch):
    sets = []
    for i in range(3):
        s = _set()
        s["match_id"] = f"m{i}"
        sets.append(s)
    monkeypatch.setattr(coinplay_sets, "fetch_raw", lambda: {"sets": sets})
    monkeypatch.setattr(state, "already_posted", lambda keys: set())
    out = coinplay_sets.fetch("football", per_run=2)
    assert len(out) == 2


# --------------------------------------------------- generate.py integration --

def test_rows_for_honors_pre_supplied_rows():
    match = {"home": "A", "away": "B", "rows": [{"label": "X", "icon": "x",
                                                 "model": "m", "home": 1, "away": 0, "reason": ""}]}
    assert generate._rows_for(match) == match["rows"]


def test_captions_ufc_uses_pick_not_fake_score():
    match = {"home": "Fighter A", "away": "Fighter B", "vertical": "ufc",
            "competition": "UFC 300", "date": "2026-12-01"}
    rows = [{"label": "ChatGPT", "home": 1, "away": 0, "reason": "KO"},
           {"label": "Claude", "home": 1, "away": 0, "reason": "KO"}]
    caps = generate.captions(match, rows)
    assert "1–0" not in caps["x"]
    assert "Fighter A" in caps["x"]
    assert "🥊" in caps["threads"]
    assert "Your pick?" in caps["x"]


def test_captions_football_keeps_score_wording():
    match = {"home": "A", "away": "B", "vertical": "football", "competition": "", "date": ""}
    rows = [{"label": "ChatGPT", "home": 2, "away": 1, "reason": ""}]
    caps = generate.captions(match, rows)
    assert "2–1" in caps["x"]
    assert "⚽" in caps["threads"]


def test_vertical_table_files_exist_for_all_verticals():
    for vertical, path in generate._VERTICAL_TABLE.items():
        assert os.path.exists(path), f"missing table background for {vertical}: {path}"


def test_run_picks_vertical_table_over_generic_table_image(monkeypatch, tmp_path):
    """match с vertical игнорирует общий TABLE_IMAGE и берёт тематический файл,
    если не задан свой TABLE_IMAGE_<VERTICAL>."""
    monkeypatch.delenv("TABLE_IMAGE_ESPORTS", raising=False)
    monkeypatch.setenv("TABLE_IMAGE", "/should/not/be/used.jpg")
    match = {
        "home": "NAVI", "away": "G2", "competition": "IEM", "date": "2026-12-01",
        "vertical": "esports", "home_logo": "", "away_logo": "",
        "rows": [{"label": "ChatGPT", "icon": "chatgpt", "model": "m",
                 "home": 2, "away": 1, "reason": ""}],
    }
    import argparse
    args = argparse.Namespace(out=str(tmp_path), no_video=True, no_send=True, keep_temp=False)
    zpath = generate.run(match, args)
    assert os.path.exists(zpath)
