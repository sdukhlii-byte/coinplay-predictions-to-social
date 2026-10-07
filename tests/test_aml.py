"""Тесты без сети и без ключей: всё внешнее подменяется заглушками.

Запуск:  python -m pytest -q     (или  python tests/test_aml.py )
"""

import datetime
import json
import os
import sys
import wave
import zipfile

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import fixtures        # noqa: E402
import generate        # noqa: E402
import poster          # noqa: E402
import predictions     # noqa: E402
import stats           # noqa: E402
import state           # noqa: E402
import video           # noqa: E402


# ------------------------------------------------------------------ config --

def test_env_int_survives_garbage(monkeypatch):
    monkeypatch.setenv("SEGMENTS", "два")
    assert config.env_int("SEGMENTS", 1) == 1          # раньше: ValueError и падение прогона


def test_env_float_accepts_comma(monkeypatch):
    monkeypatch.setenv("HOLD_SEC", "2,5")
    assert config.env_float("HOLD_SEC", 2.0) == 2.5


def test_env_int_clamps(monkeypatch):
    monkeypatch.setenv("N", "999")
    assert config.env_int("N", 1, lo=1, hi=10) == 10


# ------------------------------------------------------------------- stats --

def _m(date, home, away, hg, ag):
    return {"Date": date, "HomeTeam": home, "AwayTeam": away,
            "FTHG": hg, "FTAG": ag, "FTR": "H" if hg > ag else "A" if ag > hg else "D"}


SEASONS = {
    "2627": [_m("15/08/2026", "A", "B", 3, 0),
             _m("22/08/2026", "A", "C", 2, 2),
             _m("01/09/2026", "A", "D", 0, 4)],
    "2526": [_m("03/05/2026", "A", "F", 5, 0),
             _m("10/05/2026", "A", "E", 1, 1)],
}


@pytest.fixture
def league(monkeypatch):
    monkeypatch.setattr(stats, "_fetch_csv",
                        lambda lg, season: stats._parse_csv(_as_csv(SEASONS.get(season, []))))
    return "E0"


def _as_csv(rows):
    head = "Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    return head + "".join(
        f'{r["Date"]},{r["HomeTeam"]},{r["AwayTeam"]},{r["FTHG"]},{r["FTAG"]},{r["FTR"]}\n'
        for r in rows)


def test_matches_sorted_newest_first(league, monkeypatch):
    monkeypatch.setattr(stats, "season_codes", lambda n=2, today=None: ["2627", "2526"])
    got = [m["when"].strftime("%d/%m/%Y") for m in stats.load_matches(league)]
    assert got == ["01/09/2026", "22/08/2026", "15/08/2026", "10/05/2026", "03/05/2026"]


def test_form_uses_most_recent_matches(league, monkeypatch):
    """Регрессия: двойной reverse в _load_matches отдавал ПЕРВЫЕ матчи сезона
    вместо последних, и последовательность шла задом наперёд."""
    monkeypatch.setattr(stats, "season_codes", lambda n=2, today=None: ["2627", "2526"])
    form = stats._form(stats.load_matches(league), "A", n=3)
    assert form["sequence"] == "LDW"       # 01/09 L, 22/08 D, 15/08 W — от свежего к старому
    assert form["played"] == 3


def test_elo_goes_forward_in_time():
    """Победа в последнем матче должна поднимать рейтинг; при обратном порядке
    расчёта знак дельты «перетекал» не туда."""
    matches = sorted(
        stats._parse_csv(_as_csv([_m("01/02/2026", "X", "Y", 1, 0),
                                  _m("01/03/2026", "X", "Y", 1, 0)])),
        key=lambda m: m["when"], reverse=True)
    elo = stats.compute_elo(matches)
    assert elo["X"] > 1500 > elo["Y"]
    assert round(elo["X"] + elo["Y"]) == 3000      # нулевая сумма


def test_league_code_is_tolerant():
    assert stats.league_code("Premier League") == "E0"
    assert stats.league_code("  premier  league ") == "E0"
    assert stats.league_code("Primera Division") == "SP1"
    assert stats.league_code("UEFA Champions League") is None


def test_team_aliases():
    assert stats._norm("Arsenal FC") == "Arsenal"
    assert stats._norm("Club Atlético de Madrid") == "Ath Madrid"   # было "Ata. Madrid" — нет в CSV
    assert stats._norm("Paris Saint-Germain FC") == "Paris SG"
    assert stats._norm("Brighton & Hove Albion FC") == "Brighton"


def test_season_codes():
    assert stats.season_codes(2, datetime.date(2026, 9, 1)) == ["2627", "2526"]
    assert stats.season_codes(2, datetime.date(2026, 3, 1)) == ["2526", "2425"]


def test_cp1252_decoding():
    assert "ö" in stats._decode("Malmö".encode("cp1252"))


# ------------------------------------------------------------- predictions --

def test_parse_picks_the_real_json_not_the_first_braces():
    """Регрессия: нежадный поиск первой пары {} цеплял пример из рассуждения."""
    text = ('Формат такой: {"home_goals": <int>, "away_goals": <int>}\n'
            'Ответ: {"home_goals": 2, "away_goals": 1, "reason": "форма"}')
    assert predictions._parse(text) == (2, 1, "форма")


def test_parse_truncated_json():
    assert predictions._parse('{"home_goals": 3, "away_goals": 0, "reason": "об') == (3, 0, "")


def test_parse_bare_score():
    assert predictions._parse("I think it ends 2-1 for the hosts") == (2, 1, "")


def test_parse_rejects_nonsense():
    with pytest.raises(predictions.ModelError):
        predictions._parse("no idea, sorry")


def test_prompt_ignores_extra_match_keys():
    """match содержит home_flag/scores/kickoff_utc — format(**match) на них падал."""
    p = predictions._build_prompt(
        {"home": "A", "away": "B", "competition": "PL", "date": "2026-10-01",
         "home_flag": "es", "scores": "", "today": "мусор"}, "")
    assert "A vs B" in p and "мусор" not in p


def test_one_dead_model_does_not_kill_the_match(monkeypatch):
    monkeypatch.setenv("MIN_MODELS", "4")
    monkeypatch.setenv("USE_STATS", "false")
    monkeypatch.setattr(predictions, "resolve_models",
                        lambda: [(lbl, ic, f"v/{ic}") for lbl, ic, *_ in predictions.SLOTS])

    def fake_ask(model, match, web, block):
        if "grok" in model:
            raise predictions.ModelError("503")
        return 2, 1, "ok"

    monkeypatch.setattr(predictions, "ask", fake_ask)
    rows = predictions.predict_all({"home": "A", "away": "B"})
    assert len(rows) == 4                        # раньше падал весь ex.map()


def test_too_few_models_is_an_error(monkeypatch):
    monkeypatch.setenv("MIN_MODELS", "5")
    monkeypatch.setenv("USE_STATS", "false")
    monkeypatch.setattr(predictions, "resolve_models",
                        lambda: [(lbl, ic, f"v/{ic}") for lbl, ic, *_ in predictions.SLOTS])
    monkeypatch.setattr(predictions, "ask",
                        lambda *a, **k: (_ for _ in ()).throw(predictions.ModelError("500")))
    with pytest.raises(predictions.ModelError):
        predictions.predict_all({"home": "A", "away": "B"})


def test_resolve_models_tolerates_null_created(monkeypatch):
    monkeypatch.delenv("MODEL_CHATGPT", raising=False)
    monkeypatch.setattr(predictions, "_catalog", lambda: [
        {"id": "openai/gpt-4o", "created": None},
        {"id": "openai/gpt-4.1", "created": 100},
        {"id": "anthropic/claude-sonnet-4.5", "created": 1},
        {"id": "google/gemini-2.5-pro", "created": 1},
        {"id": "perplexity/sonar-pro", "created": 1},
        {"id": "x-ai/grok-4", "created": 1},
    ])
    out = dict((lbl, mid) for lbl, _, mid in predictions.resolve_models())
    assert out["ChatGPT"] == "openai/gpt-4.1"    # раньше: TypeError на None < int


# ---------------------------------------------------------------- fixtures --

def _api_match(mid, status, when, home="Arsenal FC", away="Chelsea FC"):
    return {"id": mid, "status": status, "utcDate": when,
            "homeTeam": {"name": home, "crest": ""}, "awayTeam": {"name": away, "crest": ""},
            "competition": {"name": "Premier League"}}


def test_timed_matches_are_not_skipped(monkeypatch):
    """Регрессия: фильтр status=SCHEDULED прятал матчи со статусом TIMED,
    а это как раз все ближайшие игры."""
    soon = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(fixtures, "_request", lambda code, params: [_api_match(1, "TIMED", soon)])
    got = fixtures.fetch(days_ahead=7, per_run=1)
    assert got[0]["home"] == "Arsenal FC"


def test_finished_and_imminent_matches_filtered(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [
        _api_match(1, "FINISHED", (now - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        _api_match(2, "TIMED", (now + datetime.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        _api_match(3, "TIMED", (now + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ]
    monkeypatch.setattr(fixtures, "_request", lambda code, params: rows)
    got = fixtures.fetch(days_ahead=7, per_run=5)
    assert len(got) == 1 and got[0]["date"] == (now + datetime.timedelta(days=3)).date().isoformat()


def test_no_fixtures_raises_dedicated_error(monkeypatch):
    monkeypatch.setattr(fixtures, "_request", lambda code, params: [])
    with pytest.raises(fixtures.NoFixturesFound):
        fixtures.fetch()


def test_fetch_queries_per_competition_endpoint(monkeypatch):
    """Регрессия: /v4/matches?competitions=PL,PD,... не существует и молча
    игнорирует этот параметр — правильный путь per-competition
    /v4/competitions/{code}/matches, по одному запросу на лигу."""
    monkeypatch.setenv("FIXTURES_COMPETITIONS", "PL,PD")
    seen_codes = []

    def fake_request(code, params):
        seen_codes.append(code)
        assert "competitions" not in params           # раньше протекал сюда
        assert set(params) == {"dateFrom", "dateTo"}
        return []

    monkeypatch.setattr(fixtures, "_request", fake_request)
    with pytest.raises(fixtures.NoFixturesFound):
        fixtures.fetch()
    assert seen_codes == ["PL", "PD"]                 # один запрос на каждую лигу


def test_one_bad_competition_does_not_kill_the_others(monkeypatch):
    when = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setenv("FIXTURES_COMPETITIONS", "PL,ELC")

    def fake_request(code, params):
        if code == "PL":
            raise RuntimeError("football-data.org отклонил ключ или тариф для лиги PL (403): ...")
        return [_api_match(1, "TIMED", when)]

    monkeypatch.setattr(fixtures, "_request", fake_request)
    got = fixtures.fetch()
    assert len(got) == 1                              # ELC всё равно нашёл матч


def test_duplicate_matches_deduped(monkeypatch):
    when = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(fixtures, "_request",
                        lambda code, params: [_api_match(7, "TIMED", when), _api_match(7, "TIMED", when)])
    assert len(fixtures.fetch(per_run=5)) == 1


# ------------------------------------------------------- posted-state dedup --

def test_already_posted_match_is_skipped(monkeypatch):
    """Регрессия: без этого ежедневный крон присылал бы один и тот же
    ближайший ещё не сыгранный матч каждый день подряд."""
    when = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [_api_match(1, "TIMED", when, "Arsenal FC", "Chelsea FC"),
            _api_match(2, "TIMED", when, "Real Madrid", "Barcelona")]
    monkeypatch.setattr(fixtures, "_request", lambda code, params: rows)

    first = fixtures.fetch(per_run=1)
    assert first[0]["home"] == "Arsenal FC"           # ближайший (тот же id) — берём первым
    state.mark_posted(first[0]["id"])

    second = fixtures.fetch(per_run=1)
    assert second[0]["home"] == "Real Madrid"         # первый уже отмечен — пропускаем его


def test_all_candidates_posted_raises_no_fixtures(monkeypatch):
    when = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(fixtures, "_request", lambda code, params: [_api_match(9, "TIMED", when)])
    state.mark_posted("9")
    with pytest.raises(fixtures.NoFixturesFound):
        fixtures.fetch()


def _api_match_comp(mid, status, when, competition, home="Team A", away="Team B"):
    return {"id": mid, "status": status, "utcDate": when,
            "homeTeam": {"name": home, "crest": ""}, "awayTeam": {"name": away, "crest": ""},
            "competition": {"name": competition}}


def test_dense_league_does_not_dominate_every_pick(monkeypatch):
    """Регрессия: «берём просто ближайший по времени матч из всех лиг»
    систематически выбирал одну и ту же лигу (например, Série A Бразилии,
    где игры почти каждый день) — не повтор матча, а перекос отбора в
    сторону лиги с более плотным календарём."""
    now = datetime.datetime.now(datetime.timezone.utc)
    soon = (now + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = (now + datetime.timedelta(days=2, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        _api_match_comp(1, "TIMED", soon, "Brasileirão Série A", "Santos FC", "São Paulo FC"),
        _api_match_comp(2, "TIMED", later, "Premier League", "Arsenal FC", "Chelsea FC"),
    ]
    monkeypatch.setattr(fixtures, "_request", lambda code, params: rows)

    # вчера уже публиковали Série A — без ротации сегодня снова выбрали бы
    # её же (id=1 раньше по времени), хотя есть свежая альтернатива.
    state.mark_posted("0", competition="Brasileirão Série A")

    got = fixtures.fetch(per_run=1)
    assert got[0]["home"] == "Arsenal FC"


def test_rotation_never_blocks_posting_when_no_alternative(monkeypatch):
    """Если во всех остальных лигах пауза — ротация не должна ронять прогон,
    просто снова берём то, что есть."""
    when = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [_api_match_comp(1, "TIMED", when, "Brasileirão Série A")]
    monkeypatch.setattr(fixtures, "_request", lambda code, params: rows)
    state.mark_posted("0", competition="Brasileirão Série A")
    state.mark_posted("00", competition="Brasileirão Série A")

    got = fixtures.fetch(per_run=1)
    assert got[0]["id"] == "1"


def test_recent_competitions_reads_newest_first(monkeypatch):
    state.mark_posted("a", competition="Premier League")
    state.mark_posted("b", competition="La Liga")
    state.mark_posted("c", competition="Serie A")
    assert state.recent_competitions(2) == ["Serie A", "La Liga"]


def test_skip_posted_can_be_disabled(monkeypatch):
    when = (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(fixtures, "_request", lambda code, params: [_api_match(5, "TIMED", when)])
    monkeypatch.setenv("FIXTURES_SKIP_POSTED", "false")
    state.mark_posted("5")
    got = fixtures.fetch()  # без дедупликации всё равно вернёт уже "опубликованный" матч
    assert got[0]["home"] == "Arsenal FC"


# ---------------------------------------------------------------- generate --

def test_slugify_non_latin_names():
    """Регрессия: кириллица давала пустой слаг, out_dir совпадал с out/,
    а архив назывался '.zip'."""
    cyr = generate.slugify("Спартак-vs-Зенит-2026-10-01")
    assert cyr and cyr != "-" and cyr == generate.slugify("Спартак-vs-Зенит-2026-10-01")
    assert cyr != generate.slugify("Динамо-vs-Зенит-2026-10-01")   # разные матчи — разные папки
    assert generate.slugify("—").startswith("match-")
    assert generate.slugify("Arsenal-vs-Chelsea-2026-10-01") == "arsenal-vs-chelsea-2026-10-01"


def test_parse_scores_errors():
    with pytest.raises(ValueError):
        generate.parse_scores("2-1,3-0", 5)
    with pytest.raises(ValueError):
        generate.parse_scores("2-1,abc,1-1,0-0,2-2", 5)
    assert generate.parse_scores("2-1, 1:0, 0–0, 3-3, 1-2", 5)[2] == (0, 0)



def test_check_date_rejects_past():
    with pytest.raises(ValueError):
        generate._check_date({"home": "A", "away": "B", "date": "2020-01-01"}, allow_past=False)
    generate._check_date({"home": "A", "away": "B", "date": "2020-01-01"}, allow_past=True)


def test_captions_scale_with_row_count():
    rows = [{"label": f"M{i}", "home": 2, "away": 1} for i in range(4)]
    caps = generate.captions({"home": "A", "away": "B", "competition": "PL", "date": "2026-10-01"},
                             rows)
    assert "(4/4 models)" in caps["threads"]      # раньше было жёстко "/5"
    assert len(caps["x"]) <= 280


def test_build_kit_writes_valid_zip(tmp_path):
    cover = tmp_path / "filled.jpg"
    Image.new("RGB", (60, 100), "black").save(cover)
    rows = [{"label": "ChatGPT", "home": 2, "away": 1}]
    z = generate.build_kit(str(tmp_path), "a-vs-b",
                           {"home": "A", "away": "B", "competition": "PL", "date": "2026-10-01"},
                           rows, "", str(cover))
    with zipfile.ZipFile(z) as zf:
        assert zf.testzip() is None
        kit = json.loads(zf.read("kit.json"))
        assert set(kit["platforms"]) == {"threads", "instagram", "x"}
        assert kit["platforms"]["x"]["images"] == ["01.jpg"]
    assert not os.path.exists(str(z) + ".tmp")


def test_load_flag_falls_back_to_placeholder(monkeypatch):
    monkeypatch.setattr(generate.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
    img = generate.load_flag("zzz", "Testonia")   # раньше сеть роняла весь матч
    assert img.size == (600, 400)


# ------------------------------------------------------------------ poster --

def _match(rows=3):
    flag = Image.new("RGBA", (600, 400), (200, 30, 30, 255))
    return poster.Match(
        home="Alpha", away="Beta", home_flag=flag, away_flag=flag,
        rows=[poster.Row(f"MODEL{i}", i % 5, (i + 1) % 5, "claude") for i in range(rows)])


def test_render_paper_shapes():
    m = _match()  # 3 строки -> полоска моделей -> канвас выше базового
    img = poster.render_paper(m, filled_rows=0)
    assert img.size == (poster.PAPER_W, poster._canvas_h(m)) and img.mode == "RGB"


def test_render_paper_base_height_without_model_strip():
    """Один агрегатный ряд (telegram_caption.py) — полоски моделей нет,
    канвас остаётся прежней базовой высоты."""
    img = poster.render_paper(_match(rows=1), filled_rows=0)
    assert img.size == (poster.PAPER_W, poster.PAPER_H)


def test_sheet_is_portrait_not_a_device_screen():
    """Простой распечатанный листок (см. референс) — портретный, ближе к
    A4, а не вытянутый экран устройства, которого больше нет."""
    ratio = poster.PAPER_W / poster.PAPER_H
    assert 0.75 < ratio < 1.1


def test_score_box_fills_only_when_requested():
    """Один бокс со счётом — главный сигнал «вот прогноз». На бланке (0)
    он должен быть пустым, на финальном кадре (1) — с цифрами."""
    m = _match(1)
    blank = poster.render_paper(m, filled_rows=0)
    filled = poster.render_paper(m, filled_rows=1)
    box = (poster.PAPER_W // 2 - poster.SCORE_BOX_W // 2, poster.SCORE_BOX_Y0,
           poster.PAPER_W // 2 + poster.SCORE_BOX_W // 2, poster.SCORE_BOX_Y0 + poster.SCORE_BOX_H)
    assert blank.crop(box).tobytes() != filled.crop(box).tobytes()


def test_blank_sheet_has_no_digits_in_the_score_box():
    """До заполнения в боксе не должно быть тёмных чернил — иначе ролику
    нечего «дописывать»: модель получает уже готовый ответ на первом кадре."""
    import numpy as np
    m = _match(1)
    box = (poster.PAPER_W // 2 - poster.SCORE_BOX_W // 4 - 60, poster.SCORE_BOX_Y0 + 30,
           poster.PAPER_W // 2 - poster.SCORE_BOX_W // 4 + 60, poster.SCORE_BOX_Y0 + poster.SCORE_BOX_H - 30)
    blank = np.asarray(poster.render_paper(m, filled_rows=0).crop(box).convert("L"))
    filled = np.asarray(poster.render_paper(m, filled_rows=1).crop(box).convert("L"))
    assert blank.min() > 200          # пустая белая бумага, никаких чернил
    assert filled.min() < 100         # а тут — тёмная цифра


def test_pretty_date_and_consensus_text():
    assert generate._pretty_date("2026-10-02") == "2 OCT 2026"
    assert generate._pretty_date("") == ""
    assert generate._pretty_date("не дата") == "не дата"   # не роняем прогон

    rows = [{"label": "A", "home": 2, "away": 1},
            {"label": "B", "home": 1, "away": 0},
            {"label": "C", "home": 0, "away": 2}]
    assert generate.consensus_text(rows, "Alpha", "Beta") == "Alpha"
    assert generate.consensus_note(rows, "Alpha", "Beta") == "2 of 3 models agree"
    assert generate.consensus_text([], "Alpha", "Beta") == ""
    assert generate.consensus_note([], "Alpha", "Beta") == ""


def test_hero_score_uses_majority_not_first_row_for_live_predictions():
    """Регрессия: раньше главный бокс всегда брал rows[0] — то, что ответил
    первый по очереди ChatGPT, а не настоящий консенсус. При живой разбивке
    из нескольких разных моделей (predictions.py) это могло противоречить
    собственной подписи "N OF M MODELS AGREE"."""
    rows = [{"label": "ChatGPT", "home": 2, "away": 1},    # rows[0] — МЕНЬШИНСТВО
            {"label": "Claude", "home": 1, "away": 1},
            {"label": "Gemini", "home": 1, "away": 1},
            {"label": "Grok", "home": 1, "away": 1}]
    h, a = generate._hero_score(rows, has_real_consensus=False, home="Alpha", away="Beta")
    assert (h, a) == (1, 1)   # самый частый счёт (3 из 4), не ответ rows[0]


def test_hero_score_keeps_rows0_when_already_aggregated():
    """Готовый консенсус (coinplay_sets.py) или единственная агрегатная
    строка (telegram_caption.py) — пересчитывать большинство не из чего и не
    нужно, rows[0] и так единственно верный ответ."""
    rows = [{"label": "COINPLAY AI PANEL", "home": 3, "away": 0}]
    assert generate._hero_score(rows, True, "Alpha", "Beta") == (3, 0)
    assert generate._hero_score(rows, False, "Alpha", "Beta") == (3, 0)
    assert generate._hero_score([], False, "Alpha", "Beta") == (None, None)


def test_score_box_shows_winner_name_for_ufc_not_fake_digits():
    """UFC кодирует победителя как 1/0 (см. coinplay_sets.rows_from_predictions)
    — на листе это обязано читаться как имя, а не как счёт "1–0", которого
    на самом деле не было."""
    flag = Image.new("RGBA", (10, 10))
    m = poster.Match(home="Alpha Fighter", away="Beta Fighter", home_flag=flag, away_flag=flag,
                     vertical="ufc", rows=[poster.Row("COINPLAY AI PANEL", 1, 0, "ai")])
    img = poster.render_paper(m, filled_rows=1)
    box = (poster.PAPER_W // 2 - poster.SCORE_BOX_W // 2 + 20, poster.SCORE_BOX_Y0 + 20,
           poster.PAPER_W // 2 + poster.SCORE_BOX_W // 2 - 20, poster.SCORE_BOX_Y0 + poster.SCORE_BOX_H - 20)
    import numpy as np
    pixels = np.asarray(img.crop(box).convert("L"))
    assert pixels.min() < 100   # имя победителя реально нарисовано тёмными чернилами


def test_model_strip_appears_only_with_real_breakdown():
    """Полоска мини-иконок моделей рисуется только когда есть реальная
    разбивка (>1 строки) — агрегатный single-row бланк (telegram_caption.py)
    её не получает, чтобы не выдумывать детализацию, которой не было."""
    flag = Image.new("RGBA", (10, 10))
    single = poster.Match(home="A", away="B", home_flag=flag, away_flag=flag,
                          rows=[poster.Row("COINPLAY AI PANEL", 2, 1, "ai")])
    multi = poster.Match(home="A", away="B", home_flag=flag, away_flag=flag,
                         rows=[poster.Row("ChatGPT", 2, 1, "chatgpt"),
                               poster.Row("Claude", 2, 1, "claude"),
                               poster.Row("Gemini", 1, 1, "gemini")])
    assert poster.render_paper(single).size[1] == poster.PAPER_H
    assert poster.render_paper(multi).size[1] == poster.PAPER_H + poster.STRIP_EXTRA_H


def test_model_strip_caps_at_max_and_shows_overflow_chip():
    flag = Image.new("RGBA", (10, 10))
    rows = [poster.Row(f"MODEL{i}", i % 3, (i + 1) % 3, "ai") for i in range(9)]
    m = poster.Match(home="A", away="B", home_flag=flag, away_flag=flag, rows=rows)
    img = poster.render_paper(m)
    # не падает и не выходит за пределы канваса с 9 моделями (> STRIP_MAX)
    assert img.size[1] == poster.PAPER_H + poster.STRIP_EXTRA_H


def test_full_run_picks_majority_score_for_live_multi_model_rows(tmp_path, monkeypatch):
    """End-to-end: generate.run() с несколькими живыми rows (--scores, без
    готового консенсуса) кладёт в m.hero_home/hero_away счёт БОЛЬШИНСТВА, а
    не ответ первой модели по списку (regression для старого rows[0]-бага)."""
    monkeypatch.setenv("TABLE_IMAGE", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", "tables", "football.jpg"))
    captured = {}
    real_render = poster.render_paper

    def spy(m, *a, **k):
        captured["hero"] = (m.hero_home, m.hero_away)
        return real_render(m, *a, **k)

    monkeypatch.setattr(poster, "render_paper", spy)
    match = {
        "home": "Alpha", "away": "Beta", "competition": "Test Cup", "date": "2026-12-01",
        "scores": "2-1,1-1,1-1,1-1,1-1",  # rows[0] (SLOTS[0]) отвечает 2-1 — МЕНЬШИНСТВО
    }
    import argparse
    args = argparse.Namespace(out=str(tmp_path), no_video=True, no_send=True, keep_temp=False)
    generate.run(match, args)
    assert captured["hero"] == (1, 1)   # счёт большинства (4 из 5), не rows[0]=(2, 1)


def test_digit_rendering_is_deterministic_for_same_seed():
    """Одинаковый seed должен рисовать цифры в боксе пиксель-в-пиксель
    одинаково — иначе blank/filled кадры одного прогона разъедутся
    чуть-чуть по-разному у каждого вызова."""
    m = _match(1)
    a = poster.render_paper(m, filled_rows=1, seed=42)
    b = poster.render_paper(m, filled_rows=1, seed=42)
    assert a.tobytes() == b.tobytes()


def test_compose_frame_size_and_bad_table_image(tmp_path):
    frame = poster.compose_frame(poster.render_paper(_match(), 1), "")
    assert frame.size == (poster.FRAME_W, poster.FRAME_H)
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    assert poster.compose_frame(poster.render_paper(_match(), 1), str(broken)).size == frame.size


def test_empty_rows_do_not_crash():
    m = poster.Match(home="A", away="B",
                     home_flag=Image.new("RGBA", (10, 10)), away_flag=Image.new("RGBA", (10, 10)),
                     rows=[])
    assert poster.render_paper(m, 0).size == (poster.PAPER_W, poster.PAPER_H)


# ------------------------------------------------------------------ video --

def test_prompt_stays_under_fal_length_limit():
    """Регрессия: fal/Kling отклоняет prompt длиннее 2500 символов (422
    'String should have at most 2500 characters')."""
    assert len(video.build_prompt("football", 2, 1, "Alpha", "Beta")) < 2500
    assert len(video.negative()) < 2500


def test_prompt_describes_one_hand_holding_sheet_no_pen():
    """Сценарий один: лист держат в руке, никакой второй руки/ручки/маркера
    в кадре — это и есть референс, который попросили."""
    prompt = video.build_prompt("football", 2, 1, "Alpha", "Beta").lower()
    assert "one hand holds up" in prompt
    assert "no pen or marker" in prompt or "no second hand" in prompt
    assert '"2"' in prompt and '"1"' in prompt
    assert "hand" in video.negative().lower()


def test_prompt_ufc_reveals_winner_name_not_digits():
    """UFC: в боксе нет счёта (см. poster._score_box) — промпт должен
    описывать появление ИМЕНИ победителя, а не цифр "1"/"0"."""
    prompt = video.build_prompt("ufc", 1, 0, "Alpha", "Beta").lower()
    assert '"alpha"' in prompt
    assert '"1"' not in prompt and '"0"' not in prompt


def _tiny_image():
    return Image.new("RGB", (4, 4))


def test_generate_segment_dispatches_to_openrouter(monkeypatch):
    monkeypatch.setenv("VIDEO_PROVIDER", "or-veo31lite")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(video, "_or_submit", lambda model, prompt, first, last: calls.append(model) or "job1")
    monkeypatch.setattr(video, "_or_poll", lambda job_id: {"unsigned_urls": ["https://x/video.mp4"]})
    monkeypatch.setattr(video, "_download",
                        lambda url, out_path, headers=None: calls.append(("dl", url, out_path, headers)))
    monkeypatch.setattr(video, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fal не должен вызываться")))

    video.generate_segment(_tiny_image(), _tiny_image(), "prompt", "/tmp/out.mp4")
    assert calls[0] == "google/veo-3.1-lite"
    kind, url, out_path, headers = calls[1]
    assert (kind, url, out_path) == ("dl", "https://x/video.mp4", "/tmp/out.mp4")
    # Регрессия: "unsigned_urls" от OpenRouter на деле требуют тот же Bearer,
    # что и submit/poll — без заголовка скачивание падает 401 уже ПОСЛЕ того,
    # как генерация оплачена, и run() валится, так и не пометив матч
    # опубликованным (отсюда был повтор одного и того же матча изо дня в день).
    assert headers is not None
    assert headers["Authorization"] == "Bearer test-key"


def test_generate_segment_dispatches_to_fal(monkeypatch):
    monkeypatch.setenv("VIDEO_PROVIDER", "kling25")
    monkeypatch.setattr(video, "_run", lambda endpoint, payload: {"video": {"url": "https://x/v.mp4"}})
    monkeypatch.setattr(video, "_download", lambda url, out_path, headers=None: None)
    monkeypatch.setattr(video, "_or_submit",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("openrouter не должен вызываться")))

    assert video.generate_segment(_tiny_image(), _tiny_image(), "prompt", "/tmp/out.mp4") == "/tmp/out.mp4"


def test_unknown_video_provider_lists_both_backends(monkeypatch):
    monkeypatch.setenv("VIDEO_PROVIDER", "does-not-exist")
    with pytest.raises(video.VideoError) as exc:
        video.generate_segment(_tiny_image(), _tiny_image(), "prompt", "/tmp/out.mp4")
    assert "or-veo31lite" in str(exc.value) and "kling25" in str(exc.value)


def test_or_submit_payload_shape(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"id": "job123"}

    def fake_post(url, headers, json, timeout):
        captured["url"], captured["headers"], captured["json"] = url, headers, json
        return FakeResp()

    monkeypatch.setattr(video.requests, "post", fake_post)
    job_id = video._or_submit("google/veo-3.1-lite", "a prompt", _tiny_image(), _tiny_image())

    assert job_id == "job123"
    assert captured["url"] == f"{video.OR_API}/videos"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    body = captured["json"]
    assert body["model"] == "google/veo-3.1-lite"
    assert body["aspect_ratio"] == "9:16"
    types = {f["frame_type"] for f in body["frame_images"]}
    assert types == {"first_frame", "last_frame"}


def test_assemble_applies_music_offset(monkeypatch, tmp_path):
    # MUSIC_OFFSET должен попасть в atrim ДО amix, а не быть проигнорирован —
    # иначе трек всегда звучит с начала файла и пик не подвести под вердикт.
    monkeypatch.setenv("MUSIC_OFFSET", "12")
    monkeypatch.setenv("MUSIC_VOLUME", "0.3")
    calls = []

    def fake_ff(*args):
        calls.append(args)
        # эмулируем результат ffmpeg — конечный файл должен появиться
        out = args[-1]
        if out.endswith(".mp4"):
            with open(out, "wb") as f:
                f.write(b"0")

    monkeypatch.setattr(video, "_ff", fake_ff)
    monkeypatch.setattr(video, "_has_audio", lambda p: False)
    monkeypatch.setattr(video, "ensure_tools", lambda: None)

    seg = tmp_path / "seg0.mp4"
    seg.write_bytes(b"0")
    music = tmp_path / "music.mp3"
    music.write_bytes(b"0")
    out = tmp_path / "out.mp4"

    video.assemble([str(seg)], str(out), hold_sec=2.0, music=str(music))

    mix_call = calls[-1]
    filter_complex = mix_call[mix_call.index("-filter_complex") + 1]
    assert "atrim=start=12.0" in filter_complex
    assert "volume=0.3" in filter_complex


def test_assemble_bakes_in_hold_clip_and_stinger(monkeypatch, tmp_path):
    # hold_sec > 0 -> должен появиться отдельный _hold.mp4 (Ken Burns +
    # дзынь), а не просто tpad-заморозка внутри одного прохода ffmpeg.
    calls = []

    def fake_ff(*args):
        calls.append(args)
        out = args[-1]
        if out.endswith(".mp4"):
            with open(out, "wb") as f:
                f.write(b"0")

    monkeypatch.setattr(video, "_ff", fake_ff)
    monkeypatch.setattr(video, "_has_audio", lambda p: False)
    monkeypatch.setattr(video, "ensure_tools", lambda: None)

    seg = tmp_path / "seg0.mp4"
    seg.write_bytes(b"0")
    out = tmp_path / "out.mp4"

    video.assemble([str(seg)], str(out), hold_sec=2.0, music="")

    hold_calls = [c for c in calls if any(str(a).endswith("_hold.mp4") for a in c)]
    assert hold_calls, "ожидался отдельный вызов ffmpeg, строящий _hold.mp4"
    zoompan_calls = [c for c in calls if any("zoompan" in str(a) for a in c)]
    assert zoompan_calls, "ожидался zoompan (наезд камеры) в фильтрах стоп-кадра"


def test_assemble_skips_hold_clip_when_hold_sec_is_zero(monkeypatch, tmp_path):
    calls = []

    def fake_ff(*args):
        calls.append(args)
        out = args[-1]
        if out.endswith(".mp4"):
            with open(out, "wb") as f:
                f.write(b"0")

    monkeypatch.setattr(video, "_ff", fake_ff)
    monkeypatch.setattr(video, "_has_audio", lambda p: False)
    monkeypatch.setattr(video, "ensure_tools", lambda: None)

    seg = tmp_path / "seg0.mp4"
    seg.write_bytes(b"0")
    out = tmp_path / "out.mp4"

    video.assemble([str(seg)], str(out), hold_sec=0.0, music="")

    assert not any("zoompan" in str(a) for c in calls for a in c)


def test_stinger_wav_writes_a_real_wav_file(tmp_path):
    out = tmp_path / "stinger.wav"
    video._stinger_wav(str(out))
    assert out.exists() and out.stat().st_size > 1000
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() > 0


def test_pick_music_prefers_explicit_music_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_FILE", "assets/beat.mp3")
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path))
    assert video.pick_music() == "assets/beat.mp3"


def test_pick_music_picks_from_dir_and_is_stable_per_seed(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSIC_FILE", raising=False)
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path))
    for name in ("a.mp3", "b.mp3", "c.wav"):
        (tmp_path / name).write_bytes(b"0")

    first = video.pick_music(seed="match-42")
    second = video.pick_music(seed="match-42")
    assert first == second  # тот же матч -> тот же трек при повторном прогоне
    assert os.path.dirname(first) == str(tmp_path)


def test_pick_music_empty_when_nothing_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSIC_FILE", raising=False)
    monkeypatch.delenv("MUSIC_DIR", raising=False)
    assert video.pick_music() == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_headline_counts_only_models_shown_on_sheet():
    m = _match(rows=0)
    names = ["deepseek", "qwen", "chatgpt", "gemini", "claude", "grok", "perplexity",
             "llama", "mistral", "kimi"]
    m.rows = [poster.Row(n.upper(), 2, 0, n) for n in names]
    m.consensus_note = "10 of 10 models agree"   # сырой счёт голосов на лист не попадает
    m.consensus_pct = 100
    assert poster._headline(m) == "5 TOP AI MODELS PREDICT"
    shown = [r.icon for r in poster._strip_rows(m)]
    assert shown == ["chatgpt", "gemini", "claude", "grok", "perplexity"]
    assert poster._agree_text(m) == "ALL MODELS AGREE"


def test_agree_text_percent_and_fallback_to_note():
    m = _match(rows=3)
    m.consensus_pct = 80
    assert poster._agree_text(m) == "80% OF MODELS AGREE"
    m.consensus_pct = 0
    m.consensus_note = "4 of 5 models agree"
    assert poster._agree_text(m) == "80% OF MODELS AGREE"
    m.consensus_note = ""
    assert poster._agree_text(m) == ""


def test_headline_single_row_has_no_count():
    assert poster._headline(_match(rows=1)) == "AI PREDICTION"
