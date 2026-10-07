"""Тесты telegram_caption.py на реальных текстах постов (скопированы из
скриншотов Telegram-канала CoinPlay AI — football/ufc/esports)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate               # noqa: E402
import telegram_caption as tc  # noqa: E402

FOOTBALL_TEXT = """
🤖 COINPLAY AI PREDICTIONS

Bayer Leverkusen vs RB Leipzig
Bundesliga · Football

🎯 Primary call: Bayer Leverkusen 2-1
✅ Winner: Bayer Leverkusen

📊 Panel: 10 of 10 lean Bayer Leverkusen
Mean model confidence: Bayer Leverkusen 57% · RB Leipzig 22% · Draw 21%

🧠 Quick read:
Bayer Leverkusen favored on model consensus; likely 2-1, low upset risk

🔮 Confidence: Medium
⚠️ Danger: Low

🎮 Play on Coinplay

18+ · analysis & entertainment predictions · play responsibly
"""

UFC_TEXT = """
🤖 COINPLAY AI PREDICTIONS

Ian Escuza vs Luca Borando
Dana White's Contender Series: Season 10, Week 8 · UFC

🎯 Primary call: Ian Escuza
✅ Winner: Ian Escuza

📊 Panel: 9 of 10 lean Ian Escuza
Mean model confidence: Ian Escuza 59% · Luca Borando 41%
Coinplay AI, combined model estimate: Ian Escuza 80%

🧠 Quick read:
Ian Escuza favored on model consensus, low upset risk

🔮 Confidence: Medium
⚠️ Danger: Low

🎮 Play on Coinplay

18+ · analysis & entertainment predictions · play responsibly
"""

ESPORTS_TEXT = """
🤖 COINPLAY AI PREDICTIONS

InterActive Philippines vs Team Ivory
EPL World Series: Southeast Asia Season 18 - Dota 2 (Group Stage) · Dota 2 · Bo3

🎯 8 of 10 models pick InterActive Philippines
Most common score among those picks: 2-1
Average model confidence: InterActive Philippines 57% · Team Ivory 43%
Coinplay AI, combined model estimate: InterActive Philippines 73%

🔮 Confidence: Medium
⚠️ Risk: Medium

🎮 Play on Coinplay

18+ · analysis & entertainment predictions · play responsibly
"""


def test_parse_football():
    p = tc.parse_caption(FOOTBALL_TEXT, "football")
    assert p["home"] == "Bayer Leverkusen" and p["away"] == "RB Leipzig"
    assert p["competition"] == "Bundesliga · Football"
    assert p["winner"] == "Bayer Leverkusen"
    assert p["score"] == (2, 1)
    assert (p["panel_agree"], p["panel_total"]) == (10, 10)
    assert p["confidence_pct_by_name"] == {
        "Bayer Leverkusen": 57, "RB Leipzig": 22, "Draw": 21}
    assert p["confidence_label"] == "Medium"
    assert p["risk_label"] == "Low"


def test_parse_ufc_no_score():
    p = tc.parse_caption(UFC_TEXT, "ufc")
    assert p["home"] == "Ian Escuza" and p["away"] == "Luca Borando"
    assert p["winner"] == "Ian Escuza"
    assert p["score"] is None
    assert (p["panel_agree"], p["panel_total"]) == (9, 10)
    assert p["ai_pct"] == 80


def test_parse_esports_models_pick_wording():
    p = tc.parse_caption(ESPORTS_TEXT, "esports")
    assert p["home"] == "InterActive Philippines" and p["away"] == "Team Ivory"
    assert p["winner"] == "InterActive Philippines"
    assert p["score"] == (2, 1)
    assert (p["panel_agree"], p["panel_total"]) == (8, 10)
    assert p["ai_pct"] == 73
    assert p["risk_label"] == "Medium"


def test_parse_caption_raises_without_vs_line():
    with pytest.raises(tc.CaptionParseError):
        tc.parse_caption("random text with no match info", "football")


def test_match_from_caption_football_uses_real_score():
    m = tc.match_from_caption(FOOTBALL_TEXT, "football", match_id="t1")
    assert m["id"] == "coinplay-caption-t1"
    assert m["home"] == "Bayer Leverkusen" and m["away"] == "RB Leipzig"
    assert m["vertical"] == "football"
    assert len(m["rows"]) == 1
    row = m["rows"][0]
    assert (row["home"], row["away"]) == (2, 1)
    assert m["subtitle"] == "10 OF 10 MODELS AGREE"
    assert m["consensus"] == "BAYER LEVERKUSEN"
    assert "10 of 10 models agree" in m["consensus_note"]
    assert "Low risk" in m["consensus_note"]
    assert m["consensus_pct"] == 57  # confidence_pct_by_name, т.к. ai_pct нет


def test_match_from_caption_ufc_encodes_winner_as_1_0():
    m = tc.match_from_caption(UFC_TEXT, "ufc")
    row = m["rows"][0]
    assert (row["home"], row["away"]) == (1, 0)  # Ian Escuza — home, победитель
    assert m["consensus_pct"] == 80  # ai_pct побеждает


def test_match_from_caption_esports_uses_most_common_score():
    m = tc.match_from_caption(ESPORTS_TEXT, "esports")
    row = m["rows"][0]
    assert (row["home"], row["away"]) == (2, 1)
    assert m["subtitle"] == "8 OF 10 MODELS AGREE"


def test_match_from_file(tmp_path):
    p = tmp_path / "post.txt"
    p.write_text(FOOTBALL_TEXT, encoding="utf-8")
    m = tc.match_from_file(str(p), "football", match_id="x")
    assert m["home"] == "Bayer Leverkusen"


# --------------------------------------------------- generate.py integration --

def test_run_uses_caption_subtitle_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TABLE_IMAGE_FOOTBALL", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", "tables", "football.jpg"))
    match = tc.match_from_caption(FOOTBALL_TEXT, "football", match_id="t2")
    import argparse
    args = argparse.Namespace(out=str(tmp_path), no_video=True, no_send=True, keep_temp=False)
    zpath = generate.run(match, args)
    assert os.path.exists(zpath)
