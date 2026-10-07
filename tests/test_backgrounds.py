"""AI-фон под конкретный матч (backgrounds.py) и его подключение в
generate.run(): включается только явным AI_BACKGROUND=1, уважает
--no-ai-background, кешируется в out_dir/_bg.jpg и никогда не роняет
прогон, если генерация недоступна/падает."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backgrounds  # noqa: E402
import generate  # noqa: E402

ASSETS_TABLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "tables")


def _match(vertical="football"):
    return {
        "home": "Belgium", "away": "France", "competition": "Friendly",
        "date": "2026-12-01", "vertical": vertical,
        "rows": [{"label": "ChatGPT", "icon": "chatgpt", "model": "m",
                  "home": 2, "away": 1, "reason": ""}],
    }


def test_build_prompt_mentions_both_teams_for_football():
    p = backgrounds.build_prompt("football", "Belgium", "France")
    assert "Belgium" in p and "France" in p
    assert "9:16" in p


def test_build_prompt_ufc_uses_corners_not_crowd_sides():
    p = backgrounds.build_prompt("ufc", "Fighter A", "Fighter B")
    assert "Fighter A" in p and "Fighter B" in p
    assert "corner" in p.lower()


def test_generate_to_file_without_key_returns_false_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = tmp_path / "bg.jpg"
    assert backgrounds.generate_to_file("football", "A", "B", str(out)) is False
    assert not out.exists()


def test_generate_to_file_swallows_api_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake")

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(backgrounds, "call_openai", boom)
    out = tmp_path / "bg.jpg"
    assert backgrounds.generate_to_file("football", "A", "B", str(out)) is False
    assert not out.exists()


def test_run_ignores_ai_background_when_env_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AI_BACKGROUND", raising=False)
    monkeypatch.setenv("TABLE_IMAGE_FOOTBALL", os.path.join(ASSETS_TABLES, "football.jpg"))
    calls = []
    monkeypatch.setattr(backgrounds, "generate_to_file",
                         lambda *a, **k: calls.append(a) or True)
    args = argparse.Namespace(out=str(tmp_path), no_video=True, no_send=True,
                               keep_temp=False, no_ai_background=False,
                               fresh_background=False)
    zpath = generate.run(_match(), args)
    assert os.path.exists(zpath)
    assert calls == []  # AI_BACKGROUND не стоял -> фон под матч не генерили


def test_run_calls_ai_background_when_enabled_and_caches(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BACKGROUND", "1")
    monkeypatch.setenv("TABLE_IMAGE_FOOTBALL", os.path.join(ASSETS_TABLES, "football.jpg"))
    calls = []

    def fake_generate(vertical, home, away, out_path, api_key=""):
        calls.append((vertical, home, away))
        # имитируем успешную генерацию — копируем статичный фон
        from PIL import Image
        Image.open(os.path.join(ASSETS_TABLES, "football.jpg")).convert("RGB").save(out_path)
        return True

    monkeypatch.setattr(backgrounds, "generate_to_file", fake_generate)
    args = argparse.Namespace(out=str(tmp_path), no_video=True, no_send=True,
                               keep_temp=False, no_ai_background=False,
                               fresh_background=False)
    generate.run(_match(), args)
    assert len(calls) == 1
    assert calls[0] == ("football", "Belgium", "France")

    # повторный прогон того же матча — бланк уже есть, генерация не платится снова
    generate.run(_match(), args)
    assert len(calls) == 1


def test_run_respects_no_ai_background_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BACKGROUND", "1")
    monkeypatch.setenv("TABLE_IMAGE_FOOTBALL", os.path.join(ASSETS_TABLES, "football.jpg"))
    calls = []
    monkeypatch.setattr(backgrounds, "generate_to_file",
                         lambda *a, **k: calls.append(a) or True)
    args = argparse.Namespace(out=str(tmp_path), no_video=True, no_send=True,
                               keep_temp=False, no_ai_background=True,
                               fresh_background=False)
    generate.run(_match(), args)
    assert calls == []  # --no-ai-background берёт верх над AI_BACKGROUND=1
