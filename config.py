"""Единая работа с переменными окружения.

Раньше каждый модуль делал `int(os.environ.get("SEGMENTS", "1"))` напрямую:
опечатка в переменной Railway (`SEGMENTS=two`, `HOLD_SEC=2,5`) роняла весь
процесс с ValueError ещё до первого запроса к API. Здесь любое некорректное
значение логируется и заменяется дефолтом — прогон продолжается.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("config")

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def env_bool(name: str, default: bool) -> bool:
    raw = env_str(name)
    if not raw:
        return default
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    log.warning("%s=%r — не похоже на true/false, беру %s", name, raw, default)
    return default


def env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("%s=%r — не целое число, беру %d", name, raw, default)
        return default
    return _clamp(name, val, default, lo, hi)


def env_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = env_str(name)
    if not raw:
        return default
    try:
        val = float(raw.replace(",", "."))  # "2,5" из Railway UI — частая опечатка
    except ValueError:
        log.warning("%s=%r — не число, беру %s", name, raw, default)
        return default
    return _clamp(name, val, default, lo, hi)


def _clamp(name, val, default, lo, hi):
    if lo is not None and val < lo:
        log.warning("%s=%s меньше минимума %s — беру %s", name, val, lo, lo)
        return lo
    if hi is not None and val > hi:
        log.warning("%s=%s больше максимума %s — беру %s", name, val, hi, hi)
        return hi
    return val


def env_list(name: str, default: list[str] | None = None, upper: bool = False) -> list[str]:
    raw = env_str(name)
    if not raw:
        return list(default or [])
    items = [p.strip() for p in raw.split(",") if p.strip()]
    return [i.upper() for i in items] if upper else items


def require_env(name: str, hint: str = "") -> str:
    """Понятная ошибка вместо KeyError из глубины стека."""
    val = env_str(name)
    if not val:
        msg = f"Переменная окружения {name} не задана"
        raise RuntimeError(f"{msg}. {hint}".strip())
    return val
