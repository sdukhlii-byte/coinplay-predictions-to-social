"""Фильтр постов по фразе-маркеру."""

import re

from config import POST_EXCLUDE_PHRASE, POST_FILTER_PHRASE, STRIP_FILTER_PHRASE

# Фразы приводим к нижнему регистру заранее.
_PHRASES = [p.strip().lower() for p in POST_FILTER_PHRASE.split("|") if p.strip()]
_EXCLUDE_PHRASES = [p.strip().lower() for p in POST_EXCLUDE_PHRASE.split("|") if p.strip()]

ENABLED = bool(_PHRASES)


def _normalize(text: str) -> str:
    """
    Схлопывает пробелы и переносы, чтобы фраза находилась,
    даже если в посте она разорвана переносом строки или двойным пробелом.
    """
    return re.sub(r"\s+", " ", (text or "").lower())


def excluded(text: str) -> bool:
    """True, если текст — служебная заглушка и публиковаться не должен ни при каких условиях."""
    if not _EXCLUDE_PHRASES or not text:
        return False
    normalized = _normalize(text)
    return any(phrase in normalized for phrase in _EXCLUDE_PHRASES)


def matches(text: str) -> bool:
    """Проходит ли пост фильтр. Без настроенных фраз проходят все."""
    if excluded(text):
        return False
    if not ENABLED:
        return True
    normalized = _normalize(text)
    return any(phrase in normalized for phrase in _PHRASES)


def strip_phrase(text: str) -> str:
    """Убирает фразу-маркер из текста, если это включено настройкой."""
    if not (ENABLED and STRIP_FILTER_PHRASE and text):
        return text

    result = text
    for phrase in _PHRASES:
        # \s+ между словами — фраза могла быть разорвана переносом строки.
        pattern = r"\s*".join(re.escape(w) for w in phrase.split())
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)

    # Подчищаем осевшие пустые строки, пробелы и висящую пунктуацию.
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"[ \t]+([,.!?;:])", r"\1", result)
    result = re.sub(r"[ \t]+\n", "\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
