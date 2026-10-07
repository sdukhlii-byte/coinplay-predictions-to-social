"""Персистентное состояние: какие матчи уже публиковались.

Зачем: `fixtures.fetch()` всегда берёт ближайший по времени матч из окна
[сейчас, +FIXTURES_DAYS_AHEAD]. Если запускать `--auto` раз в день по крону,
а матч ещё не сыгран (например, до него 3 дня), это БУДЕТ ТОТ ЖЕ САМЫЙ матч
на следующий день — без дедупликации в группу каждый день уходил бы дубль
одного и того же прогноза, пока матч наконец не пройдёт.

Файл переживает между отдельными прогонами --auto, ТОЛЬКО если лежит на
persistent volume (в Railway: Settings -> Volumes, примонтировать том на
путь, совпадающий с POSTED_STATE_FILE / его директорией, по умолчанию
/app/state/posted.json). Без тома контейнер каждый раз стартует с чистого
диска, и дедупликация не работает — тогда стоит либо примонтировать том,
либо просто знать, что редкий повтор возможен.
"""

from __future__ import annotations

import json
import logging
import os
import time

from config import env_str

log = logging.getLogger("state")

RETENTION_DAYS = 60  # не даём файлу расти вечно


def _path() -> str:
    return env_str(
        "POSTED_STATE_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "posted.json"),
    )


def _load() -> dict:
    p = _path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        log.warning("posted-state %s битый (%s) — начинаю с чистого листа", p, e)
        return {}


def already_posted(keys: set) -> set:
    """Какие из переданных ключей уже отмечены как опубликованные."""
    if not keys:
        return set()
    return set(_load()) & keys


def _entry_ts(v) -> float:
    # старый формат — голое число (timestamp); новый — {"ts":..., "seq":..., "competition":...}
    return v.get("ts", 0) if isinstance(v, dict) else (v if isinstance(v, (int, float)) else 0)


def _entry_seq(v) -> float:
    # seq, а не ts — несколько mark_posted() подряд (как в одном --auto
    # прогоне или в тесте) вполне попадают в одну и ту же секунду, и по
    # голому timestamp «самый свежий» было бы не отличить.
    return v.get("seq", 0) if isinstance(v, dict) else 0


def recent_competitions(n: int = 3) -> list[str]:
    """Турниры последних `n` опубликованных матчей, от самого свежего к старому.

    Нужно, чтобы fixtures.fetch() мог избегать подряд идущих постов из одной
    лиги: без этого при простом выборе «ближайший матч по времени» плотные
    календари (например, Серия A Бразилии почти каждый день) забивают собой
    все остальные лиги — не потому что кто-то повторяется, а потому что у
    этой лиги банально больше кандидатов на роль «самый близкий».
    """
    data = _load()
    entries = [(k, v) for k, v in data.items() if isinstance(v, dict) and v.get("competition")]
    entries.sort(key=lambda kv: _entry_seq(kv[1]), reverse=True)
    return [v["competition"] for _, v in entries[:n]]


def has_marker(name: str) -> bool:
    """Одноразовые флаги рядом с posted.json (например «baseline уже снят»).

    Отдельным файлом, а не ключом в posted.json: тот чистится по
    RETENTION_DAYS, и флаг бы через 60 дней пропал."""
    return os.path.exists(os.path.join(os.path.dirname(_path()) or ".", f"{name}.marker"))


def set_marker(name: str) -> None:
    d = os.path.dirname(_path()) or "."
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{name}.marker"), "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        log.warning("Не удалось записать маркер %s (%s)", name, e)


def mark_many(keys, competition: str = "") -> None:
    """Пометить сразу много ключей одной записью файла (baseline: сотни
    исторических наборов — по одному mark_posted() это сотни перезаписей)."""
    keys = [k for k in keys if k]
    if not keys:
        return
    p = _path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    data = _load()
    seq = max((_entry_seq(v) for v in data.values()), default=0)
    now = int(time.time())
    for k in keys:
        seq += 1
        data[k] = {"ts": now, "seq": seq, "competition": competition}
    cutoff = now - RETENTION_DAYS * 86400
    data = {k: v for k, v in data.items() if _entry_ts(v) >= cutoff}
    tmp = f"{p}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except OSError as e:
        log.warning("Не удалось сохранить posted-state %s (%s)", p, e)


def mark_posted(key: str, competition: str = "") -> None:
    if not key:
        return
    p = _path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    data = _load()
    seq = max((_entry_seq(v) for v in data.values()), default=0) + 1
    data[key] = {"ts": int(time.time()), "seq": seq, "competition": competition}
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    data = {k: v for k, v in data.items() if _entry_ts(v) >= cutoff}
    tmp = f"{p}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except OSError as e:
        # состояние — best-effort: не срывать успешный прогон из-за диска
        log.warning("Не удалось сохранить posted-state %s (%s)", p, e)
