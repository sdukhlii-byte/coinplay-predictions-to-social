"""Фоновый воркер: публикует созревшие пачки, делает ретраи, продлевает токен."""

import json
import logging
import os
import re
import threading
import time

import db
import hashtags
import instagram_api
import post_filter
import selector
import telegram_source
import threads_api
import x_api
from config import (
    ALLOW_EMPTY_TEXT,
    BURST_WAIT_SECONDS,
    INSTAGRAM_CAPTION_LIMIT,
    INSTAGRAM_ENABLED,
    INSTAGRAM_HASHTAGS,
    INSTAGRAM_SELECT_STRATEGY,
    MAX_ATTEMPTS,
    SELECT_STRATEGY,
    STRIP_HASHTAGS,
    TEXT_WAIT_SECONDS,
    THREADS_ENABLED,
    THREADS_HASHTAGS,
    THREADS_TEXT_LIMIT,
    WORKER_INTERVAL_SECONDS,
    X_ENABLED,
    X_HASHTAGS,
    X_LONG_TEXT_MODE,
    X_SELECT_STRATEGY,
    X_TEXT_LIMIT,
)

log = logging.getLogger("worker")

_stop = threading.Event()

TOKEN_CHECK_INTERVAL = 12 * 3600
MEDIA_TTL_SECONDS = 3 * 24 * 3600
_last_token_check = 0.0
_last_cleanup = 0.0


def _backoff(attempts: int) -> float:
    return time.time() + min(60 * (2 ** attempts), 3600)


def _clean_text(text: str) -> str:
    text = post_filter.strip_phrase(text)
    if STRIP_HASHTAGS:
        text = re.sub(r"(?m)^\s*#\S+(\s+#\S+)*\s*$", "", text)
        text = re.sub(r"\s+#\S+", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _finalize_threads(text: str, media_entries: list):
    text = _clean_text(text)
    text = hashtags.append(text, THREADS_HASHTAGS, THREADS_TEXT_LIMIT)
    media = [
        {"kind": m["kind"], "url": telegram_source.media_public_url(m["key"])}
        for m in media_entries
    ]
    if not text and not media:
        return None
    if not text and not ALLOW_EMPTY_TEXT:
        return None
    log.info("Threads: публикую, медиа %d", len(media))
    return threads_api.publish(text, media)


def _finalize_instagram(text: str, media_entries: list):
    text = _clean_text(text)
    text = hashtags.append(text, INSTAGRAM_HASHTAGS, INSTAGRAM_CAPTION_LIMIT)
    media = [
        {"kind": m["kind"], "url": telegram_source.media_public_url(m["key"])}
        for m in media_entries
    ]
    # Instagram, в отличие от Threads/X, не публикует пост без медиа.
    if not media:
        log.info("IG: нет медиа — пропускаю площадку")
        return None
    log.info("Instagram: публикую, медиа %d — %s", len(media), [m["url"] for m in media])
    return instagram_api.publish(text, media)


def _finalize_x(text: str, media_entries: list):
    text = _clean_text(text)

    # Резервируем место под хештеги, иначе они не влезут после сжатия.
    tags = hashtags.parse(X_HASHTAGS)
    reserve = len(" ".join(tags)) + 2 if tags else 0

    if X_LONG_TEXT_MODE == "skip" and len(text) + reserve > X_TEXT_LIMIT:
        log.info("X: текст %d симв. не влезает в %d — пропускаю площадку",
                 len(text), X_TEXT_LIMIT)
        return None

    if X_LONG_TEXT_MODE != "thread":
        text = x_api.fit(text, X_TEXT_LIMIT - reserve)

    text = hashtags.append(text, X_HASHTAGS, X_TEXT_LIMIT)

    if not text and not media_entries:
        return None
    if not text and not ALLOW_EMPTY_TEXT:
        return None

    log.info("X: публикую (%d симв.), медиа %d", len(text), len(media_entries))
    return x_api.publish(text, media_entries)


FINALIZERS = {
    "threads": _finalize_threads,
    "instagram": _finalize_instagram,
    "x": _finalize_x,
}


def _publish_threads(burst: dict, candidates: list) -> list:
    chosen = selector.choose(burst.get("manifest", ""), candidates, SELECT_STRATEGY)
    if not chosen:
        return None
    text = chosen.get("text", "")
    media_entries = _resolve_media(chosen)
    return _finalize_threads(text, media_entries)


def _publish_instagram(burst: dict, candidates: list) -> list:
    chosen = selector.choose(
        burst.get("manifest", ""), candidates, INSTAGRAM_SELECT_STRATEGY
    )
    if not chosen:
        return None
    text = chosen.get("text", "")
    media_entries = _resolve_media(chosen)
    return _finalize_instagram(text, media_entries)


def _publish_x(burst: dict, candidates: list) -> list:
    chosen = selector.choose(burst.get("manifest", ""), candidates, X_SELECT_STRATEGY)
    if not chosen:
        return None
    text = chosen.get("text", "")
    media_entries = _resolve_media(chosen)
    return _finalize_x(text, media_entries)


def _resolve_media(chosen: dict) -> list:
    """Медиа пачки; если его нет — тянем по ссылкам из манифеста."""
    entries = chosen.get("media", [])
    if entries or not chosen.get("manifest_links"):
        return entries

    log.info("Картинок в пачке нет, тяну %d шт. по ссылкам из манифеста",
             len(chosen["manifest_links"]))
    try:
        return telegram_source.fetch_by_links_sync(chosen["manifest_links"])
    except Exception as e:
        log.error("Не удалось забрать картинки по ссылкам: %s", e)
        return []


PUBLISHERS = []
if THREADS_ENABLED:
    PUBLISHERS.append(("threads", _publish_threads))
if INSTAGRAM_ENABLED:
    PUBLISHERS.append(("instagram", _publish_instagram))
if X_ENABLED:
    PUBLISHERS.append(("x", _publish_x))


def _has_publishable_text(candidates: list) -> bool:
    return any(
        c.get("text") and post_filter.matches(c["text"])
        for c in candidates
    )


def _process_zip_burst(burst: dict):
    """
    Пачка из готового zip-набора: текст и картинки уже разложены по площадкам
    в kit.json, выбирать вариант (как для старого формата) не нужно —
    публикуем то, что есть, сразу.
    """
    try:
        kit = json.loads(burst.get("kit") or "{}")
    except Exception as e:
        db.mark_skipped(burst["id"], f"не смог прочитать kit: {e}")
        return

    platforms = kit.get("platforms", {})

    if not PUBLISHERS:
        db.mark_skipped(burst["id"], "не включена ни одна площадка")
        return

    done = db.get_results(burst["id"])
    pending = [(name, fn) for name, fn in PUBLISHERS if name not in done]

    if not pending:
        db.mark_posted(burst["id"], done.get("threads", []))
        return

    errors = []
    for name, _legacy_fn in pending:
        plat = platforms.get(name)
        if not plat:
            log.info("Пачка %s: в zip нет варианта для %s — пропускаю площадку",
                      burst["id"][:8], name)
            continue
        try:
            ids = FINALIZERS[name](plat.get("text", ""), plat.get("media", []))
            if ids is None:
                continue
            db.save_result(burst["id"], name, ids)
            log.info("%s: опубликовано %s", name, ids)
        except Exception as e:
            log.error("%s: ошибка публикации — %s", name, e)
            errors.append(f"{name}: {e}")

    if errors:
        raise RuntimeError("; ".join(errors))

    results = db.get_results(burst["id"])
    if not results:
        log.info("Пачка %s: публиковать нечего (ни одна площадка не совпала с kit)",
                  burst["id"][:8])
        db.mark_skipped(burst["id"], "ни одна включённая площадка не нашлась в zip")
        return

    db.mark_posted(burst["id"], results.get("threads", []))


def _process_burst(burst: dict):
    if burst.get("kit"):
        _process_zip_burst(burst)
        return

    candidates = json.loads(burst["candidates"] or "[]")

    if not PUBLISHERS:
        db.mark_skipped(burst["id"], "не включена ни одна площадка")
        return

    # Текста ещё нет. Если пачку открыл манифест, значит источник пришлёт
    # текст следом — иногда с задержкой в минуту. Ждём вместо публикации
    # пустышки, иначе текст создаст новую пачку и уйдёт без картинок.
    if not _has_publishable_text(candidates):
        age = time.time() - burst["created_at"]
        if burst.get("manifest") and age < TEXT_WAIT_SECONDS:
            db.reopen_burst(burst["id"], time.time() + BURST_WAIT_SECONDS)
            log.info("Пачка %s: жду текст (%.0f сек из %d)",
                     burst["id"][:8], age, TEXT_WAIT_SECONDS)
            return

        log.info("Пачка %s: публиковать нечего", burst["id"][:8])
        db.mark_skipped(burst["id"], "нет текста с фразой-маркером")
        return

    # Что уже улетело в прошлые попытки — не публикуем повторно.
    done = db.get_results(burst["id"])
    pending = [(name, fn) for name, fn in PUBLISHERS if name not in done]

    if not pending:
        db.mark_posted(burst["id"], done.get("threads", []))
        return

    errors = []
    nothing_to_post = 0

    for name, publish_fn in pending:
        try:
            ids = publish_fn(burst, candidates)
            if ids is None:
                nothing_to_post += 1
                continue
            db.save_result(burst["id"], name, ids)
            log.info("%s: опубликовано %s", name, ids)
        except Exception as e:
            log.error("%s: ошибка публикации — %s", name, e)
            errors.append(f"{name}: {e}")

    if errors:
        # Часть площадок могла отработать успешно — она уже записана в results
        # и в повторной попытке участвовать не будет.
        raise RuntimeError("; ".join(errors))

    results = db.get_results(burst["id"])
    if not results:
        log.info("Пачка %s: публиковать нечего", burst["id"][:8])
        db.mark_skipped(burst["id"], "нет текста с фразой-маркером")
        return

    db.mark_posted(burst["id"], results.get("threads", []))


def _cleanup_media():
    """Удаляет скачанные файлы старше TTL, чтобы Volume не разрастался."""
    for entry in db.old_media(MEDIA_TTL_SECONDS):
        try:
            if os.path.exists(entry["path"]):
                os.remove(entry["path"])
        except Exception as e:
            log.warning("Не удалось удалить %s: %s", entry["path"], e)
        db.delete_media(entry["key"])


def _tick():
    global _last_token_check, _last_cleanup

    db.requeue_stuck()

    for burst in db.claim_ready_bursts():
        try:
            _process_burst(burst)
        except Exception as e:
            attempts = burst["attempts"] + 1
            log.error("Ошибка публикации %s (попытка %d): %s",
                      burst["id"][:8], attempts, e)
            db.mark_retry(burst["id"], str(e), _backoff(burst["attempts"]), MAX_ATTEMPTS)

    now = time.time()

    if now - _last_token_check > TOKEN_CHECK_INTERVAL:
        _last_token_check = now
        if THREADS_ENABLED:
            try:
                threads_api.refresh_token_if_needed()
            except Exception as e:
                log.warning("Проверка токена Threads не удалась: %s", e)
        if INSTAGRAM_ENABLED:
            try:
                instagram_api.refresh_token_if_needed()
            except Exception as e:
                log.warning("Проверка токена Instagram не удалась: %s", e)

    if now - _last_cleanup > 6 * 3600:
        _last_cleanup = now
        _cleanup_media()


def _loop():
    log.info("Воркер запущен, интервал %d сек", WORKER_INTERVAL_SECONDS)
    while not _stop.is_set():
        try:
            _tick()
        except Exception as e:
            log.exception("Сбой в цикле воркера: %s", e)
        _stop.wait(WORKER_INTERVAL_SECONDS)


def start():
    threading.Thread(target=_loop, daemon=True, name="worker").start()


def stop():
    _stop.set()
