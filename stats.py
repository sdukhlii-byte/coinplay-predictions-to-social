"""Реальная статистика клубных матчей с football-data.co.uk — подмешивается
в промпт моделям, чтобы прогноз счёта опирался на цифры, а не только на
то, что модель нашла в вебе.

football-data.co.uk отдаёт бесплатные CSV по сезону и коду лиги, без
ключа API: https://www.football-data.co.uk/mmz4281/{season}/{league}.csv
Покрывает только клубные лиги (не сборные, не эсенспорт) — для матчей
сборных/турниров вроде ЧМ этот модуль просто ничего не найдёт, и промпт
уйдёт без статистики.

Считаем:
  * Elo обеих команд (свой расчёт по всей скачанной истории — CSV не даёт
    готового рейтинга);
  * форму (W/D/L) и голы за последние 5 матчей каждой команды;
  * личные встречи (H2H) за всё время, что есть в скачанных сезонах.

ВАЖНО о порядке матчей: внутри модуля список `matches` всегда отсортирован
от НОВЫХ к СТАРЫМ по реальной дате из CSV. Раньше сортировки по дате не
было вовсе (сезоны просто конкатенировались, а список разворачивался дважды),
из-за чего «последние 5 матчей» оказывались первыми матчами сезона, а Elo
считался в обратном хронологическом порядке.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import os
import re
import time
import unicodedata
from collections import defaultdict

import requests

from config import env_int

log = logging.getLogger("stats")

BASE = "https://www.football-data.co.uk/mmz4281"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "football-data")
CACHE_TTL = 12 * 3600
HTTP_TIMEOUT = 20

# Код лиги football-data.co.uk по названию турнира (свободный текст из
# match["competition"]; сравнение идёт по нормализованному ключу, см. _norm_key).
LEAGUE_CODES = {
    "premier league": "E0", "epl": "E0", "english premier league": "E0",
    "championship": "E1", "efl championship": "E1",
    "la liga": "SP1", "laliga": "SP1", "primera division": "SP1",
    "la liga santander": "SP1", "spanish la liga": "SP1",
    "segunda division": "SP2", "la liga 2": "SP2",
    "bundesliga": "D1", "1 bundesliga": "D1", "german bundesliga": "D1",
    "2 bundesliga": "D2",
    "serie a": "I1", "italian serie a": "I1",
    "serie b": "I2",
    "ligue 1": "F1", "french ligue 1": "F1",
    "ligue 2": "F2",
    "eredivisie": "N1",
    "primeira liga": "P1", "liga portugal": "P1",
    "scottish premiership": "SC0", "premiership": "SC0",
    "super lig": "T1", "turkish super lig": "T1",
    "jupiler pro league": "B1", "belgian pro league": "B1",
}

# Псевдонимы клубов: как встречается у пользователя/в новостях/в football-data.org
# (fixtures.py) -> как в CSV football-data.co.uk. Ключи — нормализованные
# (_norm_key), поэтому регистр, точки и диакритика значения не имеют.
ALIASES = {
    # England
    "man city": "Man City", "manchester city": "Man City",
    "man utd": "Man United", "manchester united": "Man United", "man united": "Man United",
    "spurs": "Tottenham", "tottenham hotspur": "Tottenham",
    "wolves": "Wolves", "wolverhampton wanderers": "Wolves",
    "arsenal": "Arsenal", "chelsea": "Chelsea", "liverpool": "Liverpool",
    "newcastle united": "Newcastle", "newcastle": "Newcastle",
    "west ham united": "West Ham", "west ham": "West Ham",
    "aston villa": "Aston Villa",
    "brighton hove albion": "Brighton", "brighton and hove albion": "Brighton",
    "brighton": "Brighton",
    "nottingham forest": "Nott'm Forest", "notts forest": "Nott'm Forest",
    "crystal palace": "Crystal Palace", "everton": "Everton", "fulham": "Fulham",
    "brentford": "Brentford", "bournemouth": "Bournemouth",
    "afc bournemouth": "Bournemouth", "leeds united": "Leeds", "leeds": "Leeds",
    "sheffield united": "Sheffield United", "burnley": "Burnley",
    "ipswich town": "Ipswich", "leicester city": "Leicester",
    "southampton": "Southampton", "sunderland": "Sunderland",
    # Spain
    "atletico madrid": "Ath Madrid", "atletico de madrid": "Ath Madrid",
    "club atletico de madrid": "Ath Madrid", "atletico": "Ath Madrid",
    "real madrid": "Real Madrid", "barcelona": "Barcelona", "barca": "Barcelona",
    "real sociedad": "Sociedad", "real sociedad de futbol": "Sociedad",
    "real betis balompie": "Betis", "real betis": "Betis", "betis": "Betis",
    "sevilla": "Sevilla", "villarreal": "Villarreal",
    "athletic club": "Ath Bilbao", "athletic bilbao": "Ath Bilbao",
    "valencia": "Valencia", "celta de vigo": "Celta", "rc celta de vigo": "Celta",
    "rayo vallecano": "Vallecano", "rayo vallecano de madrid": "Vallecano",
    "deportivo alaves": "Alaves", "girona": "Girona", "getafe": "Getafe",
    "ca osasuna": "Osasuna", "osasuna": "Osasuna", "rcd mallorca": "Mallorca",
    "rcd espanyol de barcelona": "Espanol", "espanyol": "Espanol",
    # Italy
    "inter": "Inter", "internazionale": "Inter", "internazionale milano": "Inter",
    "ac milan": "Milan", "milan": "Milan", "juventus": "Juventus",
    "as roma": "Roma", "roma": "Roma", "ss lazio": "Lazio", "lazio": "Lazio",
    "ssc napoli": "Napoli", "napoli": "Napoli", "atalanta": "Atalanta",
    "bc atalanta": "Atalanta", "acf fiorentina": "Fiorentina", "fiorentina": "Fiorentina",
    "torino": "Torino", "bologna": "Bologna", "udinese calcio": "Udinese",
    "genoa cfc": "Genoa", "us lecce": "Lecce", "hellas verona": "Verona",
    # France
    "psg": "Paris SG", "paris saint germain": "Paris SG", "paris saint-germain": "Paris SG",
    "olympique de marseille": "Marseille", "marseille": "Marseille",
    "olympique lyonnais": "Lyon", "lyon": "Lyon", "as monaco": "Monaco", "monaco": "Monaco",
    "lille osc": "Lille", "lille": "Lille", "ogc nice": "Nice", "nice": "Nice",
    "stade rennais 1901": "Rennes", "rennes": "Rennes", "rc lens": "Lens", "lens": "Lens",
    "stade brestois 29": "Brest", "fc nantes": "Nantes", "toulouse": "Toulouse",
    # Germany
    "bayern munich": "Bayern Munich", "bayern": "Bayern Munich",
    "bayern munchen": "Bayern Munich", "fc bayern munchen": "Bayern Munich",
    "dortmund": "Dortmund", "borussia dortmund": "Dortmund",
    "rb leipzig": "RB Leipzig", "leipzig": "RB Leipzig",
    "bayer 04 leverkusen": "Leverkusen", "bayer leverkusen": "Leverkusen",
    "leverkusen": "Leverkusen", "vfb stuttgart": "Stuttgart", "stuttgart": "Stuttgart",
    "eintracht frankfurt": "Ein Frankfurt", "sport verein werder bremen": "Werder Bremen",
    "werder bremen": "Werder Bremen", "borussia monchengladbach": "M'gladbach",
    "vfl wolfsburg": "Wolfsburg", "1 fsv mainz 05": "Mainz", "mainz": "Mainz",
    "sc freiburg": "Freiburg", "freiburg": "Freiburg", "fc augsburg": "Augsburg",
    "tsg 1899 hoffenheim": "Hoffenheim", "hoffenheim": "Hoffenheim",
    "1 fc union berlin": "Union Berlin", "union berlin": "Union Berlin",
    # Netherlands / Portugal
    "ajax": "Ajax", "afc ajax": "Ajax", "psv": "PSV", "psv eindhoven": "PSV",
    "feyenoord rotterdam": "Feyenoord", "feyenoord": "Feyenoord",
    "sl benfica": "Benfica", "benfica": "Benfica", "fc porto": "Porto", "porto": "Porto",
    "sporting clube de portugal": "Sp Lisbon", "sporting cp": "Sp Lisbon",
}

# Юридические суффиксы/префиксы, которые встречаются у football-data.org,
# но не в CSV ("Arsenal FC" -> "Arsenal", "SS Lazio" -> "Lazio").
_AFFIX = r"(?:FC|CF|SAD|AFC|CFC|SC|AC|AS|SS|SSC|RC|RCD|CA|BC|US|VFL|VFB|TSG|SV|SK|OGC|ACF)"
_SUFFIX_RE = re.compile(rf"\s+{_AFFIX}\.?$", re.I)
_PREFIX_RE = re.compile(rf"^{_AFFIX}\.?\s+", re.I)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_key(s: str) -> str:
    """Ключ для словарей: без диакритики, пунктуации и лишних пробелов."""
    s = _strip_accents((s or "").lower())
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm(name: str) -> str:
    """Имя команды в написании CSV football-data.co.uk."""
    raw = (name or "").strip()
    if not raw:
        return raw
    for candidate in (raw, _SUFFIX_RE.sub("", raw), _PREFIX_RE.sub("", _SUFFIX_RE.sub("", raw))):
        hit = ALIASES.get(_norm_key(candidate))
        if hit:
            return hit
    # Ничего не знаем — отдаём имя без юридических аффиксов, часто совпадает.
    return _PREFIX_RE.sub("", _SUFFIX_RE.sub("", raw)).strip() or raw


def league_code(competition: str) -> str | None:
    return LEAGUE_CODES.get(_norm_key(competition))


def season_codes(n_seasons: int = 2, today: datetime.date | None = None) -> list[str]:
    """Текущий сезон + предыдущие, формат '2526' для 2025/26 (от нового к старому)."""
    today = today or datetime.date.today()
    # сезон стартует летом; до июля считаем, что текущий сезон начался в прошлом году
    start_year = today.year if today.month >= 7 else today.year - 1
    return [f"{(start_year - i) % 100:02d}{(start_year - i + 1) % 100:02d}"
            for i in range(max(1, n_seasons))]


_season_codes = season_codes  # обратная совместимость со старым именем


# ------------------------------------------------------------------ дата ---

def parse_date(raw: str, time_raw: str = "") -> datetime.datetime | None:
    """CSV отдаёт дату как '17/08/2024' или '17/08/24', иногда есть колонка Time."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            d = datetime.datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    t = (time_raw or "").strip()
    if t:
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                parsed = datetime.datetime.strptime(t, fmt)
                return d.replace(hour=parsed.hour, minute=parsed.minute)
            except ValueError:
                continue
    return d


# ------------------------------------------------------------------ CSV ----

def _decode(content: bytes) -> str:
    """football-data.co.uk отдаёт CSV в cp1252, а не в utf-8: без этого
    имена с диакритикой («Bodø/Glimt», «Malmö») приходили битыми и никогда
    не находились в таблице."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_csv(text: str) -> list[dict]:
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        home, away = (row.get("HomeTeam") or "").strip(), (row.get("AwayTeam") or "").strip()
        if not home or not away:
            continue
        try:
            fthg, ftag = int(row["FTHG"]), int(row["FTAG"])
        except (ValueError, KeyError, TypeError):
            continue  # матч ещё не сыгран или строка битая
        when = parse_date(row.get("Date", ""), row.get("Time", ""))
        if when is None:
            continue
        rows.append({"HomeTeam": home, "AwayTeam": away,
                     "FTHG": fthg, "FTAG": ftag, "when": when,
                     "Date": (row.get("Date") or "").strip()})
    return rows


def _fetch_csv(league: str, season: str) -> list[dict]:
    cache_path = os.path.join(CACHE_DIR, f"{league}_{season}.csv")
    if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < CACHE_TTL:
        try:
            with open(cache_path, "rb") as f:
                return _parse_csv(_decode(f.read()))
        except OSError as e:
            log.warning("Кэш %s нечитаем (%s) — качаю заново", cache_path, e)

    url = f"{BASE}/{season}/{league}.csv"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        log.warning("Не скачал %s: %s", url, e)
        return []
    if r.status_code != 200 or len(r.content) < 200:
        log.info("Нет данных %s (сезон %s, код %s)", url, season, r.status_code)
        return []

    text = _decode(r.content)
    rows = _parse_csv(text)
    if rows:  # пустой/битый ответ в кэш не кладём, иначе он залипнет на 12 часов
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = f"{cache_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, cache_path)  # атомарно: параллельный прогон не прочитает half-written файл
        except OSError as e:
            log.warning("Не записал кэш %s: %s", cache_path, e)
    return rows


def load_matches(league: str, n_seasons: int | None = None) -> list[dict]:
    """Все матчи лиги за N сезонов, отсортированные ОТ НОВЫХ К СТАРЫМ по дате."""
    n = n_seasons if n_seasons is not None else env_int("STATS_SEASONS", 2, lo=1, hi=6)
    matches: list[dict] = []
    for season in season_codes(n):
        matches.extend(_fetch_csv(league, season))
    matches.sort(key=lambda m: m["when"], reverse=True)
    return matches


_load_matches = load_matches  # обратная совместимость


# ------------------------------------------------------------------ Elo ----

def compute_elo(matches: list[dict], k: float = 24.0, home_adv: float = 60.0) -> dict:
    """Классический Elo, старт у всех с 1500.

    `matches` приходит от новых к старым, поэтому считаем по `reversed()` —
    строго от старых к новым. Раньше порядок был перепутан, и рейтинг
    получался посчитанным «задом наперёд».
    """
    elo: dict[str, float] = defaultdict(lambda: 1500.0)
    for m in reversed(matches):
        h, a = m["HomeTeam"], m["AwayTeam"]
        rh, ra = elo[h], elo[a]
        exp_h = 1 / (1 + 10 ** ((ra - (rh + home_adv)) / 400))
        score_h = 1.0 if m["FTHG"] > m["FTAG"] else 0.0 if m["FTHG"] < m["FTAG"] else 0.5
        delta = k * (score_h - exp_h)
        elo[h] += delta
        elo[a] -= delta  # Elo — игра с нулевой суммой
    return dict(elo)


_compute_elo = compute_elo


def _form(matches: list[dict], team: str, n: int = 5) -> dict:
    """Последние n матчей команды (matches уже отсортирован от новых к старым)."""
    played = []
    for m in matches:
        if m["HomeTeam"] == team:
            played.append((m["FTHG"], m["FTAG"]))
        elif m["AwayTeam"] == team:
            played.append((m["FTAG"], m["FTHG"]))
        if len(played) >= n:
            break
    if not played:
        return {}
    w = sum(1 for gf, ga in played if gf > ga)
    d = sum(1 for gf, ga in played if gf == ga)
    losses = sum(1 for gf, ga in played if gf < ga)
    total_gf = sum(gf for gf, _ in played)
    total_ga = sum(ga for _, ga in played)
    seq = "".join("W" if gf > ga else "D" if gf == ga else "L" for gf, ga in played)
    return {"played": len(played), "w": w, "d": d, "l": losses,
            "goals_for_avg": round(total_gf / len(played), 2),
            "goals_against_avg": round(total_ga / len(played), 2),
            "sequence": seq}  # слева направо: от самого свежего матча к более старому


def _h2h(matches: list[dict], home: str, away: str, n: int = 5) -> dict:
    games = [m for m in matches if {m["HomeTeam"], m["AwayTeam"]} == {home, away}][:n]
    if not games:
        return {}
    home_wins = sum(1 for m in games
                    if (m["HomeTeam"] == home and m["FTHG"] > m["FTAG"])
                    or (m["AwayTeam"] == home and m["FTAG"] > m["FTHG"]))
    draws = sum(1 for m in games if m["FTHG"] == m["FTAG"])
    avg_goals = round(sum(m["FTHG"] + m["FTAG"] for m in games) / len(games), 2)
    lines = [f'{m["when"]:%Y-%m-%d}: {m["HomeTeam"]} {m["FTHG"]}-{m["FTAG"]} {m["AwayTeam"]}'
             for m in games]
    return {"played": len(games), "home_wins": home_wins,
            "away_wins": len(games) - home_wins - draws,
            "draws": draws, "avg_goals": avg_goals, "matches": lines}


def lookup(home: str, away: str, competition: str) -> dict:
    """
    -> {} если лига не поддерживается или данных нет, иначе:
    {"home_elo","away_elo","home_form","away_form","h2h"}
    """
    code = league_code(competition)
    if not code:
        log.info("Лига %r не в списке football-data.co.uk — без статистики", competition)
        return {}

    matches = load_matches(code)
    if not matches:
        log.info("По лиге %s (%s) не пришло ни одного матча — без статистики", competition, code)
        return {}

    h, a = _norm(home), _norm(away)
    known = {m["HomeTeam"] for m in matches} | {m["AwayTeam"] for m in matches}
    missing = [orig for orig, norm in ((home, h), (away, a)) if norm not in known]
    if missing:
        log.info("Не нашёл в CSV %s (нормализовано: %s). Добавь алиас в stats.ALIASES. "
                 "Пример известных имён: %s",
                 ", ".join(repr(x) for x in missing),
                 ", ".join(repr(x) for x in (h, a)), sorted(known)[:8])
        return {}

    elo = compute_elo(matches)
    return {
        "home_elo": round(elo.get(h, 1500)),
        "away_elo": round(elo.get(a, 1500)),
        "home_form": _form(matches, h),
        "away_form": _form(matches, a),
        "h2h": _h2h(matches, h, a),
    }


def format_for_prompt(stats: dict, home: str, away: str) -> str:
    """Текстовый блок для вставки в промпт модели. Пусто, если данных нет."""
    if not stats:
        return ""
    hf, af, h2h = stats.get("home_form") or {}, stats.get("away_form") or {}, stats.get("h2h") or {}
    lines = [f"Elo rating: {home} {stats['home_elo']} vs {away} {stats['away_elo']} "
             f"(difference {stats['home_elo'] - stats['away_elo']:+d})"]
    for team, form in ((home, hf), (away, af)):
        if form:
            lines.append(f"{team} last {form['played']} (most recent first): {form['sequence']} "
                         f"({form['w']}W {form['d']}D {form['l']}L), "
                         f"avg {form['goals_for_avg']} scored / {form['goals_against_avg']} conceded")
    if h2h:
        lines.append(f"Head-to-head last {h2h['played']}: {home} won {h2h['home_wins']}, "
                     f"{away} won {h2h['away_wins']}, {h2h['draws']} draws, "
                     f"avg {h2h['avg_goals']} goals/game")
    return "\n".join(lines)
