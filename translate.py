"""Перевод текста поста перед публикацией (например, EN -> португальский для Бразилии).

Включается переменной TRANSLATE_TO (пусто = выключено, текст уходит как есть).
Переводит Claude через Anthropic Messages API по обычному HTTP (requests),
отдельный SDK не нужен.

Если перевод не получился, бросается исключение: пачка уйдёт на повторную
попытку с паузой, а не опубликуется на английском в бразильском аккаунте.
"""

import hashlib
import logging
import threading

import requests

import config

log = logging.getLogger("translate")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

_LANG_NAMES = {
    "pt-br": "Brazilian Portuguese (pt-BR)",
    "pt": "Brazilian Portuguese (pt-BR)",
    "es": "Spanish (Latin American)",
    "en": "English",
}

_SYSTEM = (
    "You translate social media posts about sports and esports predictions into {lang}. "
    "Rules:\n"
    "- Output ONLY the translated post, no comments, no quotes, no explanations.\n"
    "- Keep the structure exactly: same line breaks, blank lines and order of blocks.\n"
    "- Keep every emoji, number, score, odds, percentage, date and time as they are.\n"
    "- Keep team names, player names, tournament names and brand names "
    "(Coinplay, CoinPlay AI) unchanged.\n"
    "- Keep hashtags and @mentions exactly as written, do not translate them.\n"
    "- Translate the call to action naturally, e.g. 'Link in bio' -> 'Link na bio' "
    "for Portuguese. Never add a URL that is not in the source.\n"
    "- Use natural, colloquial sports-fan language of the target audience, "
    "not literal word-for-word translation.\n"
    "- Do not add or remove claims, guarantees or promises that are not in the source."
)

_cache: dict = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 500


class TranslateError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.TRANSLATE_TO)


def _key(text: str, lang: str) -> str:
    return hashlib.sha256(f"{lang}\n{text}".encode("utf-8")).hexdigest()


def _call_api(text: str, lang: str) -> str:
    lang_name = _LANG_NAMES.get(lang.lower(), lang)
    try:
        r = requests.post(
            API_URL,
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": config.TRANSLATE_MODEL,
                "max_tokens": 2000,
                "system": _SYSTEM.format(lang=lang_name),
                "messages": [{"role": "user", "content": text}],
            },
            timeout=60,
        )
    except requests.RequestException as e:
        raise TranslateError(f"запрос к Anthropic API не удался: {e}") from None
    if r.status_code != 200:
        raise TranslateError(f"Anthropic API {r.status_code}: {r.text[:300]}")
    try:
        parts = r.json().get("content", [])
        out = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    except ValueError:
        raise TranslateError("Anthropic API вернул не JSON") from None
    if not out:
        raise TranslateError("пустой перевод")
    # Страховка от ответа вида "Aquí está a tradução: ..." или от обрезанного вывода.
    if len(out) > max(len(text) * 4, 200):
        raise TranslateError(f"перевод подозрительно длинный ({len(out)} симв. на {len(text)})")
    return out


def maybe(text: str) -> str:
    """Переводит текст, если включён TRANSLATE_TO. Иначе возвращает его как есть."""
    if not text or not text.strip() or not enabled():
        return text
    lang = config.TRANSLATE_TO
    k = _key(text, lang)
    with _cache_lock:
        cached = _cache.get(k)
    if cached is not None:
        return cached
    out = _call_api(text, lang)
    log.info("Перевод на %s: %d -> %d симв.", lang, len(text), len(out))
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[k] = out
    return out
