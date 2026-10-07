"""Источник матчей — готовые прогнозы бота CoinPlay AI (sets.json), а не
football-data.org (fixtures.py) и не собственный опрос моделей
(predictions.py).

CoinPlay AI уже опросил свою панель моделей и посчитал консенсус для матчей
трёх вертикалей (esports/football/ufc) и опубликовал их в Telegram. Этот
модуль просто забирает готовый результат и приводит его к тому же виду
match/rows, который уже понимает generate.py — звонить в OpenRouter или
ходить на HLTV для этого не нужно.

Формат sets.json (schema=1) — по спецификации, присланной владельцем бота:
  {"schema": 1, "updated_at": "...", "boards": {...}, "sets": [...]}
Один элемент sets[]:
  match_id, vertical (esports|football|ufc), title/title_name,
  status (graded|ungraded|retired|withdrawn),
  match: {team_a, team_b, event, start_at, series_format},
  consensus: {winner, votes, mean_confidence, score, method},
  predictions: [{model, lab, winner, confidence, score, method,
                 short_rationale, rationale, correct, ...}], ...

ВАЖНО: эта схема документирована текстом от владельца API, живым ответом
сервера в этой песочнице свериться не удалось (сеть наружу блокирует прокси
окружения) — при первом реальном запуске стоит свериться с фактическим
sets.json и поправить при расхождениях.
"""

from __future__ import annotations

import datetime
import logging

import requests

import state
from config import env_str, require_env

log = logging.getLogger("coinplay_sets")

SETS_URL = "https://209.38.103.35/sets.json"
HTTP_TIMEOUT = 30

VERTICALS = ("esports", "football", "ufc")

# "как на постере" — лейблы у CoinPlay AI уже совпадают с SLOTS из
# predictions.py (ChatGPT, Claude, Gemini, Perplexity, Grok), иконка —
# просто его нижний регистр; для неизвестной лаборатории _glyph() в
# poster.py и так рисует нейтральный значок по умолчанию.
_ICON_OVERRIDES = {
    "chatgpt": "chatgpt",
    "gpt": "chatgpt",
    "claude": "claude",
    "gemini": "gemini",
    "perplexity": "perplexity",
    "grok": "grok",
}


class SetsError(RuntimeError):
    pass


class NoSetsFound(RuntimeError):
    """Нет ни одного нового подходящего набора — нормальная пауза, не сбой."""


def _headers() -> dict:
    token = require_env(
        "SETS_API_TOKEN",
        "Нужен токен CoinPlay AI sets.json API (см. инструкцию владельца бота).")
    return {"Authorization": f"Bearer {token}"}


def fetch_raw() -> dict:
    url = env_str("SETS_API_URL", SETS_URL)
    try:
        r = requests.get(url, headers=_headers(), timeout=HTTP_TIMEOUT,
                         # сертификат выписан на голый IP — обычная проверка
                         # TLS работает штатно, верификацию не отключаем
                         )
    except requests.RequestException as e:
        raise SetsError(f"Не скачал {url}: {e}") from e
    if r.status_code == 401:
        raise SetsError("401 — токен SETS_API_TOKEN не задан или неверный")
    if r.status_code == 429:
        raise SetsError("429 — слишком частые запросы (лимит ~1/сек)")
    if r.status_code >= 400:
        raise SetsError(f"{url} -> {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
    except ValueError as e:
        raise SetsError(f"Ответ {url} не похож на JSON: {e}") from e
    if not isinstance(data, dict) or "sets" not in data:
        raise SetsError(f"Неожиданная форма ответа {url}: верхний уровень без 'sets'")
    return data


def _icon_for_lab(lab: str) -> str:
    key = (lab or "").strip().lower()
    return _ICON_OVERRIDES.get(key, key.replace(" ", "")) or "ai"


def _score_pair(score: dict | None) -> tuple[int, int] | None:
    """{"team_a": n, "team_b": m} -> (n, m); None если счёта нет (UFC)."""
    if not isinstance(score, dict):
        return None
    a, b = score.get("team_a"), score.get("team_b")
    if not isinstance(a, int) or not isinstance(b, int):
        return None
    return a, b


def rows_from_predictions(preds: list, vertical: str) -> list[dict]:
    """predictions[] из набора -> [{"label","icon","model","home","away","reason"}],
    тот же формат, что и predictions.predict_all() в живом пайплайне.

    У UFC счёта нет (score всегда null) — вместо выдуманного счёта кодируем
    то, что реально предсказано: победитель получает 1, проигравший 0. Это
    честно (мы не придумываем цифры, которых не было) и при этом укладывается
    в существующий рендер таблицы "дом/гость" без изменений в poster.py.
    """
    rows = []
    for p in preds or []:
        if not isinstance(p, dict) or p.get("winner") is None:
            continue  # abstained — модель не ответила, это не промах, но и не строка
        lab = p.get("lab") or p.get("model") or "AI"
        pair = _score_pair(p.get("score"))
        if pair is not None:
            home, away = pair
        else:
            is_a = p["winner"] == "team_a"
            home, away = (1, 0) if is_a else (0, 1)
        reason = (p.get("short_rationale") or p.get("rationale") or "")[:160]
        if vertical == "ufc" and p.get("method"):
            reason = f"{p['method']}" + (f" — {reason}" if reason else "")
        rows.append({
            "label": str(lab), "icon": _icon_for_lab(lab), "model": p.get("model", ""),
            "home": home, "away": away, "reason": reason,
        })
    return rows


def _consensus_override(s: dict, rows: list[dict]) -> dict:
    """Готовый консенсус CoinPlay AI (а не пересчитанный из rows) — это тот
    же вывод, что уже показан в самом Telegram-посте, так что цифры на видео
    совпадают с тем, что люди уже видели в канале."""
    cons = s.get("consensus") or {}
    winner = cons.get("winner")
    match = s.get("match") or {}
    team_a, team_b = match.get("team_a", ""), match.get("team_b", "")
    name = {"team_a": team_a, "team_b": team_b, "draw": "Draw"}.get(winner, "")
    votes = cons.get("votes") or {}
    total = sum(v for v in votes.values() if isinstance(v, (int, float))) or len(rows) or 1
    n = votes.get(winner, 0) if isinstance(votes.get(winner), (int, float)) else 0
    pct = round(100 * n / total) if total else 0
    return {
        "consensus": name.upper(),
        "consensus_note": f"{n} of {total} models agree" if n else "",
        "consensus_pct": pct,
    }


def match_from_set(s: dict) -> dict | None:
    """Один элемент sets[] -> словарь match, который понимает generate.run().

    Возвращает None, если в наборе нет ни одной полезной строки (все модели
    abstained) — такой набор рендерить нечего.
    """
    vertical = s.get("vertical", "")
    m = s.get("match") or {}
    team_a, team_b = m.get("team_a", ""), m.get("team_b", "")
    if not (team_a and team_b):
        return None

    rows = rows_from_predictions(s.get("predictions") or [], vertical)
    if not rows:
        return None

    start_raw = (m.get("start_at") or "").strip()
    date = ""
    if start_raw:
        try:
            date = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            log.warning("%s: start_at %r не распарсил", s.get("match_id"), start_raw)

    competition = m.get("event") or ""
    bo = m.get("series_format")
    if bo:
        competition = f"{competition} · {bo}" if competition else bo

    match = {
        "id": f"coinplay-{s.get('match_id')}",
        "home": team_a, "away": team_b,
        "home_flag": "", "away_flag": "",
        "competition": competition, "date": date,
        "vertical": vertical,
        "rows": rows,  # забирает _rows_for() в generate.py вместо живого опроса моделей
    }
    match.update(_consensus_override(s, rows))
    return match


# Статусы, при которых набор уже не «свежий предикт»: матч сыгран/снят.
SKIP_STATUSES = ("graded", "retired", "withdrawn")


def set_key(s: dict) -> str:
    return f"coinplay-{s.get('match_id')}"


def watch_sets(data: dict, verticals) -> list[dict]:
    """Наборы нужных вертикалей, ещё актуальные (не сыграны/не сняты), в
    порядке публикации — от старых к новым, чтобы видео выходили в том же
    порядке, что и посты в канале."""
    return [s for s in (data.get("sets") or [])
            if isinstance(s, dict) and s.get("vertical") in verticals
            and s.get("status") not in SKIP_STATUSES and s.get("match_id") is not None]


def fetch(vertical: str, per_run: int = 1) -> list[dict]:
    """Свежие (ещё не отрисованные) наборы по одной вертикали, от новых к старым."""
    if vertical not in VERTICALS:
        raise ValueError(f"vertical должен быть одним из {VERTICALS}, получено {vertical!r}")

    data = fetch_raw()
    sets = [s for s in (data.get("sets") or [])
           if isinstance(s, dict) and s.get("vertical") == vertical
           and s.get("status") not in ("withdrawn",)]
    if not sets:
        raise NoSetsFound(f"В sets.json нет наборов вертикали {vertical!r}")

    # sets отданы от старых публикаций к новым (см. спецификацию) — новые нам
    # интереснее, и именно они совпадут с тем, что CoinPlay AI запостил
    # последним в канал.
    sets = list(reversed(sets))

    ids = {f"coinplay-{s.get('match_id')}" for s in sets}
    already = state.already_posted(ids)

    matches = []
    for s in sets:
        key = f"coinplay-{s.get('match_id')}"
        if key in already:
            continue
        match = match_from_set(s)
        if match is None:
            continue
        matches.append(match)
        if len(matches) >= per_run:
            break

    if not matches:
        raise NoSetsFound(f"Все новые наборы {vertical!r} уже отрисованы или пусты")
    return matches
