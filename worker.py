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
import stats
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
    SOURCE_HASHTAGS,
    STATS_ENABLED,
    STATS_INTERVAL_DAYS,
    STATS_RETRY_COOLDOWN_SECONDS,
    STRIP_HASHTAGS,
    TEXT_WAIT_SECONDS,
    THREADS_ENABLED,
    THREADS_HASHTAGS,
    THREADS_REQUIRE_IMAGE,
    THREADS_TEXT_LIMIT,
    WORKER_INTERVAL_SECONDS,
    X_ENABLED,
    X_HASHTAGS,
    X_LONG_TEXT_MODE,
    X_REQUIRE_IMAGE,
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


# Голый URL в тексте Threads/Instagram не становится кликабельной ссылкой —
# вместо этого Threads цепляет к нему карточку-превью, и если текст из-за
# этого не влезает в лимит и уходит "связанным тредом" (см. threads_api.
# split_text), превью-карточка оказывается на отдельном посте вида "(2/2)".
# Источник в части постов (например, UFC-кит) пишет реальную ссылку вместо
# принятого у остальных "Link in bio" — из-за этого одни посты расползаются
# на два, а другие (без ссылки в тексте) публикуются одним постом.
_URL_RE = re.compile(r"(?:https?://\S+|www\.\S+|\b[a-zA-Z0-9][\w-]*\.[a-zA-Z]{2,6}/\S+)")


def _strip_links(text: str) -> str:
    """Заменяет сырые ссылки в тексте на тот же 'Link in bio', что и так пишет источник."""
    new_text, n = _URL_RE.subn("Link in bio", text)
    if not n:
        return text
    new_text = re.sub(r"(Link in bio)(?:[ \t]*\n?[ \t]*\1)+", r"\1", new_text)
    new_text = re.sub(r"[ \t]{2,}", " ", new_text)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip()
    return new_text


def _hashtags_for(platform: str, chat_id, default_raw: str) -> str:
    """
    Теги для конкретной площадки+источника. Если для этого chat_id в
    SOURCE_HASHTAGS заданы свои теги — берём их (иначе, если источник там
    есть, но площадка в нём не указана — тоже общий default_raw), иначе
    общий набор по умолчанию (THREADS_HASHTAGS/X_HASHTAGS/INSTAGRAM_HASHTAGS).
    """
    per_source = SOURCE_HASHTAGS.get(str(chat_id)) if chat_id is not None else None
    if per_source and platform in per_source:
        return per_source[platform]
    return default_raw


def _finalize_threads(text: str, media_entries: list, chat_id=None):
    if THREADS_REQUIRE_IMAGE and not any(m.get("kind") == "image" for m in media_entries):
        log.info("Threads: в пачке нет картинки — пропускаю площадку (THREADS_REQUIRE_IMAGE=true)")
        return None

    text = _clean_text(text)
    text = _strip_links(text)
    text = hashtags.append(text, _hashtags_for("threads", chat_id, THREADS_HASHTAGS), THREADS_TEXT_LIMIT)
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


def _finalize_instagram(text: str, media_entries: list, chat_id=None):
    text = _clean_text(text)
    text = hashtags.append(text, _hashtags_for("instagram", chat_id, INSTAGRAM_HASHTAGS), INSTAGRAM_CAPTION_LIMIT)
    media = [
        {"kind": m["kind"], "url": telegram_source.media_public_url(m["key"])}
        for m in media_entries
    ]
    # Instagram, в отличие от Threads/X, не публикует пост без медиа.
    if not media:
        log.info("IG: у пачки нет медиа — пропускаю площадку")
        return None
    log.info("Instagram: публикую, медиа %d — %s", len(media), [m["url"] for m in media])
    return instagram_api.publish(text, media)


def _finalize_x(text: str, media_entries: list, chat_id=None):
    text = _clean_text(text)
    raw_tags = _hashtags_for("x", chat_id, X_HASHTAGS)

    # В X нужны только посты с картинкой — ни голого текста, ни видео.
    # (Видео туда в принципе почти никогда не должно долетать: генератор
    # видео сейчас отключён через VIDEO_KIT_ENABLED, а тут — подстраховка
    # на случай, если оно всё же придёт с другого источника.)
    if X_REQUIRE_IMAGE and not any(m.get("kind") == "image" for m in media_entries):
        log.info("X: в пачке нет картинки — пропускаю площадку (X_REQUIRE_IMAGE=true)")
        return None

    # Резервируем место под хештеги, иначе они не влезут после сжатия.
    tags = hashtags.parse(raw_tags)
    reserve = len(" ".join(tags)) + 2 if tags else 0

    if X_LONG_TEXT_MODE == "skip" and len(text) + reserve > X_TEXT_LIMIT:
        log.info("X: текст %d симв. не влезает в %d — пропускаю площадку",
                 len(text), X_TEXT_LIMIT)
        return None

    if X_LONG_TEXT_MODE != "thread":
        text = x_api.fit(text, X_TEXT_LIMIT - reserve)

    text = hashtags.append(text, raw_tags, X_TEXT_LIMIT)

    if not text and not media_entries:
        return None
    if not text and not ALLOW_EMPTY_TEXT:
        return None

    log.info("X: публикую (%d симв.), медиа %d", len(text), len(media_entries))
    return x_api.publish(text, media_entries)


FINALIZERS = {"threads": _finalize_threads, "instagram": _finalize_instagram, "x": _finalize_x}


def _publish_threads(burst: dict, candidates: list) -> list:
    chosen = selector.choose(burst.get("manifest", ""), candidates, SELECT_STRATEGY)
    if not chosen:
        return None
    media_entries = _resolve_media(chosen)
    log.info("Threads: публикую msg %s, медиа %d",
             chosen.get("message_id"), len(media_entries))
    return _finalize_threads(chosen.get("text", ""), media_entries, burst.get("chat_id"))


def _publish_instagram(burst: dict, candidates: list) -> list:
    chosen = selector.choose(
        burst.get("manifest", ""), candidates, INSTAGRAM_SELECT_STRATEGY
    )
    if not chosen:
        return None
    media_entries = _resolve_media(chosen)
    log.info("Instagram: публикую msg %s, медиа %d",
             chosen.get("message_id"), len(media_entries))
    return _finalize_instagram(chosen.get("text", ""), media_entries, burst.get("chat_id"))


def _publish_x(burst: dict, candidates: list) -> list:
    chosen = selector.choose(burst.get("manifest", ""), candidates, X_SELECT_STRATEGY)
    if not chosen:
        return None
    media_entries = _resolve_media(chosen)
    log.info("X: публикую msg %s, медиа %d",
             chosen.get("message_id"), len(media_entries))
    return _finalize_x(chosen.get("text", ""), media_entries, burst.get("chat_id"))


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
            ids = FINALIZERS[name](plat.get("text", ""), plat.get("media", []), burst.get("chat_id"))
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
        log.info("Пачка %s: публиковать нечего (ни одна площадка не совпала с zip)",
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

    if STATS_ENABLED:
        # last_sent обновляется только при успехе — этим меряем недельный
        # интервал. last_attempt обновляется при КАЖДОЙ попытке, успешной
        # или нет, — иначе если Telegram недоступен (бот не в чате, не тот
        # chat_id и т.п.), last_sent никогда не проставится, и это условие
        # будет true на каждом тике (раз в WORKER_INTERVAL_SECONDS) —
        # воркер долбит Instagram/Threads/X insights по кругу без остановки.
        # STATS_RETRY_COOLDOWN не даёт повторить попытку раньше чем через
        # STATS_RETRY_COOLDOWN_SECONDS после последней, даже неудачной.
        last_sent = float(db.get_state("stats_last_sent", "0") or "0")
        last_attempt = float(db.get_state("stats_last_attempt", "0") or "0")
        due = now - last_sent > STATS_INTERVAL_DAYS * 86400
        cooled_down = now - last_attempt > STATS_RETRY_COOLDOWN_SECONDS
        if due and cooled_down:
            db.set_state("stats_last_attempt", str(now))
            try:
                data = stats.collect(STATS_INTERVAL_DAYS)
                if stats.send_to_telegram(data):
                    db.set_state("stats_last_sent", str(now))
                    log.info("Статистика: еженедельный отчёт отправлен")
                else:
                    log.warning("Статистика: отправка не удалась, следующая попытка не раньше чем через %d сек",
                                STATS_RETRY_COOLDOWN_SECONDS)
            except Exception as e:
                log.error("Статистика: не удалось собрать/отправить отчёт: %s", e)


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
