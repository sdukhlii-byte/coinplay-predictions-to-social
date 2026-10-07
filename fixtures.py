"""Автоматический подбор РЕАЛЬНЫХ ближайших матчей — чтобы не редактировать
matches.json руками каждый день.

Источник: football-data.org (v4) — бесплатный API с расписанием топ-лиг.
Это ДРУГОЙ сервис, чем football-data.co.uk в stats.py (там только архивные
CSV с результатами, будущих матчей там нет).

Нужен бесплатный ключ (2 минуты, без карты):
  1. https://www.football-data.org/client/register
  2. FOOTBALL_DATA_API_KEY=<ключ из письма> в переменные окружения.

Без ключа --auto просто упадёт с понятной ошибкой — сгенерировать
несуществующие матчи он не может.
"""

from __future__ import annotations

import datetime
import logging

import requests

import state
from config import env_bool, env_float, env_int, env_list, require_env

log = logging.getLogger("fixtures")

API = "https://api.football-data.org/v4"
HTTP_TIMEOUT = 30

# football-data.org помечает будущие матчи двумя статусами: SCHEDULED (дата
# известна, точное время — нет) и TIMED (время подтверждено). Ближайшие матчи
# почти всегда TIMED, поэтому фильтр только по SCHEDULED пропускал как раз то,
# что нам нужно, и --auto регулярно «не находил матчей» при полном календаре.
UPCOMING = ("SCHEDULED", "TIMED")


class NoFixturesFound(RuntimeError):
    """Нет ни одного запланированного матча в окне поиска — не сбой, а нормальное
    состояние (пауза в календаре/межсезонье): вызывающий код должен тихо
    завершиться, а не падать и уходить в рестарт-луп."""


# Коды соревнований football-data.org — фактически ВСЕ клубные турниры,
# доступные на бесплатном тарифе (весь его список: PL, PD, SA, BL1, FL1, CL,
# ELC, DED, PPL, BSA — плюс WC/EC, но это разовые турниры, а не регулярные
# лиги, поэтому их сюда не добавляем). Особенно важна BSA (Бразилия): у неё
# своё, южноамериканское международное окно (CONMEBOL), почти никогда не
# совпадающее с европейским, — когда вся Европа стоит на паузе 10+ дней,
# Бразилейран обычно продолжает играть. Переопределяется через
# FIXTURES_COMPETITIONS (например "PL,CL" — только АПЛ и Лига чемпионов).
DEFAULT_COMPETITIONS = ["PL", "PD", "SA", "BL1", "FL1", "CL", "ELC", "DED", "PPL", "BSA"]


def _headers() -> dict:
    key = require_env(
        "FOOTBALL_DATA_API_KEY",
        "Бесплатный ключ: https://www.football-data.org/client/register — без него "
        "--auto не может узнать, какие матчи реально будут (а выдумывать их нельзя).")
    return {"X-Auth-Token": key}


def _parse_utc(raw: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat((raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _request(code: str, params: dict) -> list[dict]:
    """GET /v4/competitions/{code}/matches — по одной лиге за раз.

    У верхнеуровневого /v4/matches (без competition в пути) НЕТ параметра
    `competitions` — это не задокументировано нигде, и раньше он тут просто
    молча игнорировался API, отдавая пустой список для любого бесплатного
    ключа. Отсюда "не нашёл ни одного матча" даже при полном календаре топ-лиг.
    Верно — только per-competition эндпоинт (docs.football-data.org/general/v4/competition.html).
    """
    try:
        r = requests.get(f"{API}/competitions/{code}/matches", headers=_headers(),
                         params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"football-data.org недоступен ({code}): {e}") from e

    if r.status_code == 429:
        raise RuntimeError("football-data.org: превышен лимит запросов "
                           "(10/мин на бесплатном тарифе) — попробуй запустить чуть позже")
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"football-data.org отклонил ключ или тариф для лиги {code} ({r.status_code}): "
            f"{r.text[:200]}. Проверь FOOTBALL_DATA_API_KEY и что {code} доступен на бесплатном плане.")
    if r.status_code == 404:
        raise RuntimeError(f"football-data.org: код лиги {code!r} не существует")
    r.raise_for_status()
    try:
        return r.json().get("matches", []) or []
    except ValueError as e:
        raise RuntimeError(f"football-data.org вернул не JSON для {code}: {r.text[:200]}") from e


def fetch(days_ahead: int = 10, per_run: int = 1) -> list[dict]:
    """
    -> [{"home","away","home_flag","away_flag","competition","date","kickoff_utc"}, ...]

    Реальные ещё не сыгранные матчи из окна [сейчас, +days_ahead] по всем
    настроенным лигам, отсортированные по времени начала — берём первые
    `per_run` (самые близкие, чтобы прогноз был максимально свежим).
    """
    codes = env_list("FIXTURES_COMPETITIONS", DEFAULT_COMPETITIONS, upper=True)
    days_ahead = max(1, days_ahead)
    per_run = max(1, per_run)
    # Публиковать прогноз за пять минут до свистка бессмысленно: ролик ещё
    # рендерится. Пропускаем всё, что начинается слишком скоро.
    lead_hours = env_float("FIXTURES_MIN_LEAD_HOURS", 2.0, lo=0.0, hi=72.0)

    now = datetime.datetime.now(datetime.timezone.utc)
    earliest = now + datetime.timedelta(hours=lead_hours)
    today = now.date()
    params = {
        "dateFrom": today.isoformat(),
        "dateTo": (today + datetime.timedelta(days=days_ahead)).isoformat(),
        # статус не фильтруем на сервере: разные планы отдают SCHEDULED/TIMED
        # по-разному, надёжнее отфильтровать у себя
    }

    matches, failed = [], []
    for code in codes:
        try:
            matches.extend(_request(code, params))
        except RuntimeError as e:
            if "лимит запросов" in str(e):
                raise  # общий лимит на ключ — долбить остальные коды бессмысленно
            log.warning("%s: пропускаю лигу (%s)", code, e)
            failed.append(code)
    if failed and len(failed) == len(codes):
        raise RuntimeError(f"Ни одна из лиг {codes} не ответила — см. предупреждения выше "
                           "(неверный ключ, коды недоступны на тарифе и т.п.)")

    candidates, seen = [], set()
    for m in matches:
        if (m.get("status") or "").upper() not in UPCOMING:
            continue
        home, away = m.get("homeTeam") or {}, m.get("awayTeam") or {}
        if not home.get("name") or not away.get("name"):
            continue  # у football-data.org бывают TBD-команды в кубковых сетках
        kickoff = _parse_utc(m.get("utcDate", ""))
        if kickoff is None or kickoff < earliest:
            continue
        fid = str(m["id"]) if m.get("id") else f'{home["name"]}|{away["name"]}|{m.get("utcDate")}'
        if fid in seen:  # один матч может прийти дважды, если лига указана в двух кодах
            continue
        seen.add(fid)
        candidates.append({
            "id": fid,
            "home": home["name"],
            "away": away["name"],
            "home_flag": home.get("crest") or "",
            "away_flag": away.get("crest") or "",
            # Без этого поля match["vertical"] в generate.run() был пустой
            # строкой — а она используется и для выбора тематического фона
            # (TABLE_IMAGE_<VERTICAL> / assets/tables/<vertical>.jpg), и как
            # ворота для AI-фона под конкретный матч (AI_BACKGROUND=1, см.
            # backgrounds.py): без vertical оба всегда молча падали на
            # старый общий assets/table.jpg, даже при включённом
            # AI_BACKGROUND. football-data.org отдаёт только футбольные лиги,
            # так что здесь он всегда "football".
            "vertical": "football",
            # имя турнира от football-data.org уже в том же виде,
            # что и ключи stats.LEAGUE_CODES ("Premier League", "Serie A", ...)
            "competition": (m.get("competition") or {}).get("name", ""),
            "date": kickoff.date().isoformat(),
            "kickoff_utc": kickoff.isoformat(),
        })

    if not candidates:
        raise NoFixturesFound(
            f"Не нашёл ни одного запланированного матча за {days_ahead} дн. в лигах {codes} "
            f"(и не ближе чем через {lead_hours:g} ч) — либо пауза в календаре "
            "(межсезонье/международное окно), либо лиги не те. Если это повторяется часто — "
            "увеличь FIXTURES_DAYS_AHEAD или поменяй FIXTURES_COMPETITIONS.")

    candidates.sort(key=lambda c: c["kickoff_utc"])

    # При ежедневном крон-запуске ближайший матч почти всегда остаётся тем же
    # самым, пока не сыграется (окно и так далеко вперёд не смотрит) — без
    # этого фильтра в группу каждый день уходил бы дубль одного прогноза.
    if env_bool("FIXTURES_SKIP_POSTED", True):
        posted = state.already_posted({c["id"] for c in candidates})
        fresh = [c for c in candidates if c["id"] not in posted]
        if not fresh:
            raise NoFixturesFound(
                f"Нашёл {len(candidates)} матч(ей) за {days_ahead} дн., но все уже "
                "публиковались ранее (см. POSTED_STATE_FILE) — новых пока нет. Это нормально: "
                "жди, пока текущие матчи сыграются и из окна поиска появятся следующие. Чтобы "
                "отключить дедупликацию — FIXTURES_SKIP_POSTED=false.")
    else:
        fresh = candidates

    # Без этого «ближайший по времени» почти всегда оказывается одной и той же
    # лигой — не потому что кто-то повторяется (это уже отсекли выше), а
    # потому что у лиги с плотным календарём (например, Серии A Бразилии,
    # где матчи почти каждый день) банально больше кандидатов на роль
    # «самый близкий». Избегаем последних N опубликованных турниров, если
    # это оставляет хоть какой-то выбор — иначе (пауза во всех остальных
    # лигах) не блокируем публикацию, просто снова берём то, что есть.
    avoid_n = env_int("FIXTURES_AVOID_LAST_COMPETITIONS", 2, lo=0, hi=10)
    rotated = fresh
    if avoid_n:
        avoid = set(state.recent_competitions(avoid_n))
        if avoid:
            candidates_wo_recent = [c for c in fresh if c["competition"] not in avoid]
            if candidates_wo_recent:
                rotated = candidates_wo_recent

    picked = rotated[:per_run]
    log.info("Подобрано %d матч(ей) из %d кандидатов (%d уже публиковались, %d отсеяно ротацией лиг): %s",
             len(picked), len(candidates), len(candidates) - len(fresh), len(fresh) - len(rotated),
             "; ".join(f'{c["home"]} vs {c["away"]} ({c["competition"]}, {c["date"]})'
                       for c in picked))
    return picked
