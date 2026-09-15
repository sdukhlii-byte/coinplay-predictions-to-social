"""Чтение группы через юзер-сессию Telethon.

Бот не получает сообщения других ботов — это ограничение Bot API, которое
не обходится правами админа. Поэтому источник читается обычным аккаунтом.
"""

import asyncio
import logging
import mimetypes
import os
import uuid

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import db
import post_filter
import selector
from config import (
    BURST_WAIT_SECONDS,
    BURST_WINDOW_SECONDS,
    MEDIA_DIR,
    PUBLIC_BASE_URL,
    SOURCE_CHAT_IDS,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_STRING_SESSION,
)

log = logging.getLogger("source")

_client = None
_loop = None


def media_public_url(key: str) -> str:
    return f"{PUBLIC_BASE_URL}/media/{key}"


def _kind_of(message) -> str:
    """Определяет, годится ли вложение для Threads и чем оно является."""
    if message.photo:
        return "image"
    if message.video or message.gif or message.video_note:
        return "video"

    doc = getattr(message, "document", None)
    if doc:
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
    return ""


async def _save_media(message) -> list:
    """Скачивает вложение в MEDIA_DIR и регистрирует его для раздачи наружу."""
    kind = _kind_of(message)
    if not kind:
        return []

    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
    except Exception as e:
        log.error(
            "Не могу создать каталог %s: %s. "
            "Скорее всего не примонтирован Volume — медиа сохранять некуда.",
            MEDIA_DIR, e,
        )
        return []

    base = os.path.join(MEDIA_DIR, uuid.uuid4().hex)

    try:
        path = await message.download_media(file=base)
    except Exception as e:
        log.error("Не удалось скачать медиа из msg %s: %s", message.id, e)
        return []

    if not path:
        log.warning("msg %s: download_media вернул пусто", message.id)
        return []

    try:
        mime = mimetypes.guess_type(path)[0] or (
            "image/jpeg" if kind == "image" else "video/mp4"
        )
        key = db.register_media(path, kind, mime)

        # Имя файла из Telegram нужно, чтобы сопоставить картинку с манифестом.
        filename = ""
        try:
            if message.file and message.file.name:
                filename = message.file.name
        except Exception:
            pass
        if not filename:
            filename = os.path.basename(path)

        size = os.path.getsize(path) if os.path.exists(path) else 0
        log.info("Сохранено медиа %s (%s, %d КБ)", filename, kind, size // 1024)
        return [{"kind": kind, "key": key, "filename": filename}]
    except Exception as e:
        log.error("Не удалось зарегистрировать медиа из msg %s: %s", message.id, e)
        return []


async def _handle(event):
    message = event.message
    chat_id = event.chat_id

    if chat_id not in SOURCE_CHAT_IDS:
        return

    if not db.mark_seen(chat_id, message.id):
        return

    text = (message.text or message.message or "").strip()

    kind = _kind_of(message)
    # Превью ссылки — это тоже media, но вложением не является.
    is_webpage = type(getattr(message, "media", None)).__name__ == "MessageMediaWebPage"
    has_attachment = bool(message.media) and not is_webpage
    log.info(
        "Входящее msg %s: текст %d симв., вложение=%s%s",
        message.id, len(text),
        kind or "нет",
        " (тип не поддерживается)" if has_attachment and not kind else "",
    )

    media = await _save_media(message)

    if not text and not media:
        return

    grouped_id = getattr(message, "grouped_id", None)

    # Манифест открывает новый пост и закрывает предыдущий: источник шлёт
    # посты подряд, и по одному лишь таймауту их не разделить.
    if selector.is_manifest(text):
        info = selector.parse_manifest(text)
        db.close_open_bursts(chat_id)
        burst_id = db.start_burst(chat_id, text, BURST_WAIT_SECONDS)
        log.info(
            "msg %s — манифест [%s] %r: %d файл(ов) -> новая пачка %s",
            message.id, info["type"] or "без типа", info["title"],
            len(info["links"]) or len(info["filenames"]), burst_id[:8],
        )
        return

    # Текст без фразы-маркера не нужен. Сообщения с вложениями пропускаем
    # всегда: у картинок нет подписи.
    if not media and not grouped_id and not post_filter.matches(text):
        log.info("msg %s без фразы-маркера — пропускаю", message.id)
        return

    candidate = {
        "message_id": message.id,
        "text": text,
        "media": media,
        "grouped_id": grouped_id,
    }

    burst_id = db.add_candidate(
        chat_id=chat_id,
        candidate=candidate,
        burst_window=BURST_WINDOW_SECONDS,
        burst_wait=BURST_WAIT_SECONDS,
        grouped_id=grouped_id,
    )
    log.info("msg %s -> пачка %s (текст %d симв., медиа %d)",
             message.id, burst_id[:8], len(text), len(media))


async def fetch_by_links(links: list) -> list:
    """
    Запасной путь: скачать картинки по ссылкам вида https://t.me/c/<chat>/<msg>
    из манифеста. Нужен, если сами файлы почему-то не долетели
    отдельными сообщениями (например, сервис стартовал позже).

    chat_part из ссылки — это внутренний id чата без префикса -100 (формат
    t.me/c/<internal_id>/<msg_id>). Раньше тут всегда использовался
    SOURCE_CHAT_ID напрямую — с одним источником это случайно совпадало,
    с несколькими источниками манифест из чата B тянул бы картинку из
    чата A. Теперь чат восстанавливается из самой ссылки.
    """
    if not _client:
        return []

    out = []
    for chat_part, msg_id in links:
        try:
            chat_id = int(f"-100{chat_part}")
            message = await _client.get_messages(chat_id, ids=int(msg_id))
            if not message:
                continue
            saved = await _save_media(message)
            out.extend(saved)
        except Exception as e:
            log.warning("Не удалось забрать msg %s по ссылке: %s", msg_id, e)
    return out


def fetch_by_links_sync(links: list, timeout: int = 180) -> list:
    """
    Синхронная обёртка для вызова из воркера: он работает в отдельном потоке,
    а Telethon живёт в основном цикле asyncio.
    """
    if not _loop or not links:
        return []
    future = asyncio.run_coroutine_threadsafe(fetch_by_links(links), _loop)
    return future.result(timeout=timeout)


async def start():
    """Запускает клиент и вешает обработчик новых сообщений."""
    global _client, _loop

    _loop = asyncio.get_running_loop()

    _client = TelegramClient(
        StringSession(TELEGRAM_STRING_SESSION),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )
    _client.add_event_handler(_handle, events.NewMessage(chats=SOURCE_CHAT_IDS))

    await _client.start()
    me = await _client.get_me()
    log.info("Подключён как %s (id %s)", me.username or me.first_name, me.id)

    for chat_id in SOURCE_CHAT_IDS:
        try:
            entity = await _client.get_entity(chat_id)
            log.info("Слушаю источник: %s (%s)", getattr(entity, "title", chat_id), chat_id)
        except Exception as e:
            log.error("Не удалось получить чат %s: %s", chat_id, e)

    asyncio.create_task(_client.run_until_disconnected())


async def stop():
    if _client:
        await _client.disconnect()


async def list_dialogs():
    """Вспомогательное: показать доступные чаты с их id."""
    out = []
    async for dialog in _client.iter_dialogs():
        out.append({"id": dialog.id, "title": dialog.name})
    return out
