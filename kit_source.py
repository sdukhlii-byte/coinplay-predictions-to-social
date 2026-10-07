"""Источник матчей — zip-кит бота CoinPlay AI («social kit: A vs B»).

Бот публикует в группу «CoinPlay approvals» zip на каждое событие. Внутри:
  blank/blank.json   — schema 2: команды, турнир, 10 «лабораторий» с победителем
                       и счётом каждой (cells), match_id, sha256 бланка
  blank/blank.png, blank/blank-prompt.txt — пустой A4-бланк и промпт «рука
                       вписывает счета» (здесь НЕ используются: сцена генератора —
                       лист в руке с одним боксом, цифры гарантируют первый и
                       последний кадр, см. generate.run)
  instagram/caption.txt, x/post.txt — готовые подписи (берём только средний
                       confidence, остальное считаем из blank.json)
  kit.json           — match_id и платформы

Всё, что нужно генератору, лежит в blank.json — парсить текст поста не нужно.
Дата матча берётся из имени архива: «furia-vs-auroragaming-1791297000.zip» —
суффикс это unix-время старта матча (UTC).
"""

from __future__ import annotations

import collections
import datetime
import json
import logging
import os
import re
import zipfile

import telegram_caption
from coinplay_sets import _icon_for_lab

log = logging.getLogger("kit_source")

BLANK_JSON = "blank/blank.json"
_EPOCH_RE = re.compile(r"-(\d{9,11})(?:\.zip)?$")
_MAX_ENTRY = 5 * 1024 * 1024  # blank.json/caption — килобайты; защита от zip-бомбы


class KitError(ValueError):
    pass


def _read(z: zipfile.ZipFile, name: str, required: bool = True) -> str:
    try:
        info = z.getinfo(name)
    except KeyError:
        if required:
            raise KitError(f"в ките нет {name}") from None
        return ""
    if info.file_size > _MAX_ENTRY:
        raise KitError(f"{name} подозрительно большой ({info.file_size} байт)")
    return z.read(name).decode("utf-8", errors="replace")


_FOOTBALL = ("football", "soccer")
_UFC = ("ufc", "mma")
_ESPORTS = ("cs2", "cs:go", "csgo", "counter-strike", "counter strike", "dota", "valorant",
            "league of legends", "lol", "rocket league", "rainbow six", "overwatch",
            "starcraft", "call of duty", "apex", "esport")


def _vertical(blank: dict) -> str:
    """Вертикаль по полю title («CS2», «Football», «UFC»). Неизвестный спорт
    (теннис, баскетбол …) -> "" — лучше отказать, чем нарисовать видео не той
    вертикали с чужим фоном и уйти в чужую группу."""
    t = (blank.get("title") or "").strip().lower()
    if not t:
        return "esports" if blank.get("series_format") else ""
    if any(k in t for k in _UFC):
        return "ufc"
    if any(k in t for k in _FOOTBALL):
        return "football"
    if any(k in t for k in _ESPORTS):
        return "esports"
    return ""


def _cells(lab: dict, team_a: str, team_b: str) -> tuple[int, int] | None:
    cells = lab.get("cells")
    if isinstance(cells, (list, tuple)) and len(cells) == 2:
        try:
            return int(str(cells[0]).strip()), int(str(cells[1]).strip())
        except ValueError:
            pass
    # счёта нет (UFC) — кодируем победителя как 1/0, как и coinplay_sets.py
    w = (lab.get("winner") or "").strip().lower()
    if w and w == team_a.strip().lower():
        return 1, 0
    if w and w == team_b.strip().lower():
        return 0, 1
    return None


def start_date_from_name(name: str) -> str:
    """'…/furia-vs-auroragaming-1791297000.zip' -> '2026-10-06' (UTC) или ''."""
    m = _EPOCH_RE.search(os.path.basename(name))
    if not m:
        return ""
    try:
        return datetime.datetime.fromtimestamp(
            int(m.group(1)), datetime.timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def match_from_kit(zip_path: str) -> dict:
    """zip-кит бота -> словарь match, который понимает generate.run()."""
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise KitError(f"{zip_path}: не открыл как zip ({e})") from e
    with z:
        try:
            blank = json.loads(_read(z, BLANK_JSON))
        except ValueError as e:
            raise KitError(f"{BLANK_JSON}: не JSON ({e})") from e
        caption = _read(z, "instagram/caption.txt", required=False) \
            or _read(z, "x/post.txt", required=False)
        try:
            kit_meta = json.loads(_read(z, "kit.json", required=False) or "{}")
        except ValueError:
            kit_meta = {}

    team_a, team_b = (blank.get("team_a") or "").strip(), (blank.get("team_b") or "").strip()
    if not (team_a and team_b):
        raise KitError("в blank.json нет team_a/team_b")
    if blank.get("schema") not in (2, None):
        log.warning("blank.json schema=%r — ожидалась 2, разбираю как есть", blank.get("schema"))
    vertical = _vertical(blank)
    if not vertical:
        raise KitError(f"вертикаль {blank.get('title')!r} не поддерживается "
                       "(есть football, esports, ufc)")

    rows, picks = [], []
    for lab in blank.get("labs") or []:
        if not isinstance(lab, dict):
            continue
        pair = _cells(lab, team_a, team_b)
        if pair is None:
            continue  # модель воздержалась / нет данных
        name = str(lab.get("name") or lab.get("model") or "AI")
        rows.append({"label": name, "icon": _icon_for_lab(name), "model": lab.get("model", ""),
                     "home": pair[0], "away": pair[1], "reason": ""})
        picks.append((lab.get("winner") or "", pair))
    if not rows:
        raise KitError("в blank.json нет ни одной модели со счётом")

    # Консенсус: у кого больше голосов; главный счёт — самый частый среди
    # проголосовавших за него (то же правило, что в подписи бота: «Most common
    # score among those picks»), при равенстве — по порядку лабораторий.
    votes = collections.Counter(w for w, _ in picks if w)
    winner = votes.most_common(1)[0][0] if votes else ""
    agree, total = (votes[winner] if winner else 0), len(rows)
    scores = collections.Counter(p for w, p in picks if w == winner)
    order = [p for w, p in picks if w == winner]
    hero = max(scores, key=lambda s: (scores[s], -order.index(s))) if scores else None

    pct = None
    cm = telegram_caption._MEAN_CONF_RE.search(caption)
    if cm:
        by_name = telegram_caption._parse_pct_list(cm.group(1))
        pct = next((v for k, v in by_name.items() if k.lower() == winner.lower()), None)
    if pct is None and total:
        pct = round(100 * agree / total)

    event = (blank.get("event") or "").strip()
    bo = (blank.get("series_format") or "").strip()
    competition = f"{event} · {bo}" if event and bo else (event or bo)
    key = blank.get("match_id") or kit_meta.get("match_id") or os.path.splitext(
        os.path.basename(zip_path))[0]

    return {
        "id": f"coinplay-kit-{key}",
        "home": team_a, "away": team_b, "home_flag": "", "away_flag": "",
        "competition": competition,
        "date": start_date_from_name(zip_path),
        "vertical": vertical,
        "rows": rows,
        "hero": hero,
        "consensus": winner.upper(),
        "consensus_note": f"{agree} of {total} models agree" if agree else "",
        "consensus_pct": pct or 0,
    }
