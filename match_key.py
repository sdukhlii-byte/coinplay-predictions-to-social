"""Ключ матча и тип поста — для защиты от дублей.

Один и тот же матч приходит в сервис несколькими путями (zip-кит, манифест +
фото, старый текст без манифеста). Чтобы не публиковать его на одну площадку
дважды, у каждого поста считается:

  * ключ матча — нормализованная пара команд, порядок не важен;
  * тип поста — pred (прогноз), bet (BET BUILDER / COMBINADA) или result
    (итог матча): это разные посты, друг друга они не заменяют.
"""

import re
import unicodedata

# "Cyprus vs Armenia"; в заголовках допускаем и тире между командами.
_TITLE_VS_RE = re.compile(r"\s+(?:vs\.?|v|[-–—])\s+", re.IGNORECASE)
# Строка текста поста вида "Cyprus vs Armenia" или "Belgium vs Turkiye · Football".
_LINE_VS_RE = re.compile(
    r"^\W*?([^\n·|•]{2,60}?)\s+vs\.?\s+([^\n·|•]{2,60}?)\s*(?:[·|•].*)?$",
    re.IGNORECASE | re.UNICODE,
)
# Строки-описания вроде "10 of 10 AI models pick Belgium" командами не считаем.
_NOT_A_MATCH_LINE_RE = re.compile(
    r"\bmodels?\b|\bmodelos\b|\bpick|\beligen\b|\bconfidence\b|\bconfianza\b",
    re.IGNORECASE,
)
_KIT_PREFIX_RE = re.compile(r"^\s*(?:social\s+)?kit\s*[:\-]\s*", re.IGNORECASE)

_RESULT_RE = re.compile(r"\bresult(?:ado|s)?\b|\bfinal\s*:", re.IGNORECASE)
_BET_RE = re.compile(r"bet\s*builder|\bcombinada\b", re.IGNORECASE)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def _key(a: str, b: str):
    na, nb = norm(a), norm(b)
    if not na or not nb or na == nb:
        return None
    return "|".join(sorted((na, nb)))


def title_from_manifest(manifest: str) -> str:
    """'zipkit · social kit: A vs B' или 'threads · тип · A vs B' -> 'A vs B'."""
    head = (manifest or "").splitlines()[0] if manifest else ""
    if not head:
        return ""
    if head.lower().startswith("zipkit"):
        return head.split("·", 1)[-1].strip()
    parts = [p.strip() for p in re.split(r"\s*[·|]\s*", head)]
    if len(parts) >= 3:
        return " · ".join(parts[2:])
    return parts[1] if len(parts) == 2 else ""


def key_from_title(title: str):
    parts = _TITLE_VS_RE.split(_KIT_PREFIX_RE.sub("", title or ""), maxsplit=1)
    return _key(parts[0], parts[1]) if len(parts) == 2 else None


def key_from_text(text: str):
    """Пара команд из строки 'A vs B' в тексте поста (для старого формата без манифеста)."""
    for line in (text or "").splitlines()[:12]:
        line = line.strip()
        if not line or len(line) > 90 or _NOT_A_MATCH_LINE_RE.search(line):
            continue
        m = _LINE_VS_RE.match(line)
        if m:
            key = _key(m.group(1), m.group(2))
            if key:
                return key
    return None


def match_key(manifest: str, text: str):
    """Ключ матча: из заголовка манифеста/кита, иначе из текста. None — не удалось определить."""
    return key_from_title(title_from_manifest(manifest)) or key_from_text(text)


def post_type(text: str) -> str:
    """pred | bet | result — по первым строкам (шапка поста)."""
    head = "\n".join((text or "").splitlines()[:3])
    if _RESULT_RE.search(head):
        return "result"
    if _BET_RE.search(head):
        return "bet"
    return "pred"
