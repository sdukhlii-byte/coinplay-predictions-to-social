"""Тесты kit_source.py: zip-кит бота («social kit») -> match для generate.run()."""

import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate        # noqa: E402
import kit_source      # noqa: E402
import telegram_caption  # noqa: E402

CAPTION = """FURIA vs Aurora Gaming
ESL Pro League Season 24 (Group Stage) · CS2 · Bo3

🤖 10 of 10 AI models pick FURIA

Most common score among those picks: 2-1

📊 Average model confidence: FURIA 67% · Aurora Gaming 33%
"""

LABS = [("ChatGPT", 2, 1), ("Claude", 2, 1), ("Gemini", 2, 1), ("Grok", 2, 1),
        ("DeepSeek", 2, 1), ("Llama", 2, 0), ("Qwen", 2, 0), ("Mistral", 2, 0),
        ("Perplexity", 2, 1), ("Kimi", 2, 1)]


def _blank(labs=LABS, title="CS2", series="Bo3", winner="FURIA"):
    return {"schema": 2, "match_id": "3334a920adb8bd8e", "team_a": "FURIA",
            "team_b": "Aurora Gaming", "event": "ESL Pro League Season 24 (Group Stage)",
            "title": title, "series_format": series,
            "labs": [{"name": n, "model": f"x/{n.lower()}", "winner": winner,
                      "cells": [str(a), str(b)]} for n, a, b in labs]}


def _kit(tmp_path, blank=None, caption=CAPTION, name="furia-vs-auroragaming-1791297000.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("blank/blank.json", json.dumps(blank or _blank()))
        z.writestr("blank/blank.png", b"png")
        z.writestr("instagram/caption.txt", caption)
        z.writestr("kit.json", json.dumps({"match_id": "3334a920adb8bd8e"}))
    return str(p)


def test_match_from_real_shaped_kit(tmp_path):
    m = kit_source.match_from_kit(_kit(tmp_path))
    assert (m["home"], m["away"]) == ("FURIA", "Aurora Gaming")
    assert m["id"] == "coinplay-kit-3334a920adb8bd8e"
    assert m["vertical"] == "esports"
    assert m["date"] == "2026-10-06"                       # из epoch в имени архива
    assert m["competition"] == "ESL Pro League Season 24 (Group Stage) · Bo3"
    assert len(m["rows"]) == 10
    assert m["rows"][5] == {"label": "Llama", "icon": "llama", "model": "x/llama",
                            "home": 2, "away": 0, "reason": ""}
    assert m["hero"] == (2, 1)                              # 7 из 10 — самый частый
    assert m["consensus"] == "FURIA"
    assert m["consensus_note"] == "10 of 10 models agree"
    assert m["consensus_pct"] == 67                         # средний confidence из подписи


def test_pct_falls_back_to_vote_share_without_caption(tmp_path):
    m = kit_source.match_from_kit(_kit(tmp_path, caption=""))
    assert m["consensus_pct"] == 100


def test_hero_tie_resolved_by_lab_order(tmp_path):
    labs = [("ChatGPT", 2, 0), ("Claude", 2, 1)]
    m = kit_source.match_from_kit(_kit(tmp_path, _blank(labs)))
    assert m["hero"] == (2, 0)


def test_ufc_without_scores_encodes_winner(tmp_path):
    blank = _blank(title="UFC", series="")
    for lab in blank["labs"]:
        lab["cells"] = []
        lab["winner"] = "FURIA"
    m = kit_source.match_from_kit(_kit(tmp_path, blank))
    assert m["vertical"] == "ufc"
    assert m["hero"] == (1, 0)


def test_vertical_detection():
    assert kit_source._vertical({"title": "Football"}) == "football"
    assert kit_source._vertical({"title": "CS2", "series_format": "Bo3"}) == "esports"
    assert kit_source._vertical({"title": "UFC"}) == "ufc"
    assert kit_source._vertical({"title": "Dota 2", "series_format": "Bo3"}) == "esports"
    assert kit_source._vertical({"title": "Tennis"}) == ""


def test_unsupported_sport_is_rejected(tmp_path):
    with pytest.raises(kit_source.KitError, match="не поддерживается"):
        kit_source.match_from_kit(_kit(tmp_path, _blank(title="Tennis", series="Bo3")))


def test_broken_kits_raise_kit_error(tmp_path):
    bad = tmp_path / "x.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(kit_source.KitError):
        kit_source.match_from_kit(str(bad))
    nojson = tmp_path / "y.zip"
    with zipfile.ZipFile(nojson, "w") as z:
        z.writestr("kit.json", "{}")
    with pytest.raises(kit_source.KitError):
        kit_source.match_from_kit(str(nojson))


def test_start_date_from_name():
    assert kit_source.start_date_from_name("a/furia-vs-x-1791297000.zip") == "2026-10-06"
    assert kit_source.start_date_from_name("kit.zip") == ""


def test_cli_kit_runs_pipeline(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(generate, "run", lambda mt, args: seen.append(mt) or "x.zip")
    monkeypatch.setattr(sys, "argv", ["generate.py", "--kit", _kit(tmp_path)])
    with pytest.raises(SystemExit) as e:
        generate.main()
    assert e.value.code == 0 and seen[0]["hero"] == (2, 1)
    from state import already_posted
    assert already_posted({"coinplay-kit-3334a920adb8bd8e"})  # отмечен, повторно не рисуем


def test_caption_parser_accepts_ai_models_wording():
    p = telegram_caption.parse_caption(CAPTION, "esports")
    assert (p["panel_agree"], p["panel_total"]) == (10, 10)
    assert p["winner"] == "FURIA" and p["score"] == (2, 1)
