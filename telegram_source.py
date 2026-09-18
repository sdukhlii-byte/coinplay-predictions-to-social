"""Чтение группы через юзер-сессию Telethon.

Бот не получает сообщения других ботов — это ограничение Bot API, которое
не обходится правами админа. Поэтому источник читается обычным аккаунтом.
"""

import asyncio
import json
import logging
import mimetypes
import os
import uuid
import zipfile

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


def _is_zip(message) -> bool:
    """
    Готовый набор для соцсетей (kit.json + картинки + тексты по площадкам),
    в отличие от старого формата, приходит одним zip-документом, а не
    отдельными сообщениями с манифестом. Определяем по mime/имени файла.
    """
    doc = getattr(message, "document", None)
    if not doc:
        return False
    mime = getattr(doc, "mime_type", "") or ""
    if mime in ("application/zip", "application/x-zip-compressed"):
        return True
    try:
        if message.file and message.file.name:
            return message.file.name.lower().endswith(".zip")
    except Exception:
        pass
    return False


async def _handle_zip(message, chat_id: int) -> None:
    """
    Распаковывает готовый набор из zip (kit.json + картинки + тексты по
    площадкам) и сразу ставит пачку в очередь на публикацию — в отличие от
    старого формата (манифест + отдельные фото), тут ждать нечего: всё уже
    собрано целиком внутри архива.
    """
    tmp_dir = os.path.join(MEDIA_DIR, "kits", uuid.uuid4().hex)
    try:
        os.makedirs(tmp_dir, exist_ok=True)
    except Exception as e:
        log.error("msg %s: не могу создать каталог %s: %s", message.id, tmp_dir, e)
        return

    zip_path = os.path.join(tmp_dir, "kit.zip")
    try:
        await message.download_media(file=zip_path)
    except Exception as e:
        log.error("msg %s: не удалось скачать zip: %s", message.id, e)
        return

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)
    except Exception as e:
        log.error("msg %s: не смог распаковать zip: %s", message.id, e)
        return
    finally:
        try:
            os.remove(zip_path)
        except Exception:
            pass

    kit_path = os.path.join(tmp_dir, "kit.json")
    if not os.path.exists(kit_path):
        log.warning("msg %s: в zip нет kit.json — пропускаю", message.id)
        return

    try:
        with open(kit_path, encoding="utf-8") as f:
            kit_raw = json.load(f)
    except Exception as e:
        log.error("msg %s: не смог прочитать kit.json: %s", message.id, e)
        return

    platforms_out = {}
    for name, spec in (kit_raw.get("platforms") or {}).items():
        text = ""
        text_file = spec.get("text_file")
        if text_file:
            text_path = os.path.join(tmp_dir, name, text_file)
            if os.path.exists(text_path):
                try:
                    with open(text_path, encoding="utf-8") as f:
                        text = f.read().strip()
                except Exception as e:
                    log.warning("msg %s: не смог прочитать %s: %s", message.id, text_path, e)

        media = []
        for img_name in (spec.get("images") or []):
            img_path = os.path.join(tmp_dir, name, img_name)
            if not os.path.exists(img_path):
                log.warning("msg %s: в архиве нет %s/%s из манифеста",
                            message.id, name, img_name)
                continue
            mime = mimetypes.guess_type(img_path)[0] or "image/png"
            key = db.register_media(img_path, "image", mime)
            media.append({"kind": "image", "key": key, "filename": img_name})

        if text or media:
            platforms_out[name] = {"text": text, "media": media}

    if not platforms_out:
        log.warning("msg %s: zip не дал ни одной готовой площадки — пропускаю", message.id)
        return

    team_a = kit_raw.get("team_a")
    team_b = kit_raw.get("team_b")
    if team_a and team_b:
        title = f"{team_a} vs {team_b}"
    else:
        caption = (message.text or message.message or "").strip()
        title = caption.splitlines()[0][:80] if caption else "без названия"

    burst_id = db.start_zip_burst(
        chat_id, f"zipkit · {title}",
        {"match_id": kit_raw.get("match_id"), "platforms": platforms_out},
    )
    log.info(
        "msg %s — zip-набор %r: готово %d площадок(и) -> пачка %s, публикую без ожидания",
        message.id, title, len(platforms_out), burst_id[:8],
    )


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

    if _is_zip(message):
        await _handle_zip(message, chat_id)
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

    # Недавно добавленные чаты (только что вступили) Telethon ещё не закэшировал
    # локально — без этого get_entity(id) падает с "Could not find the input
    # entity", хотя сами сообщения из чата всё равно долетают и обрабатываются
    # штатно (роутинг в _handle идёт по голому числовому id). Обновление списка
    # диалогов нужно только для того, чтобы эта диагностика ниже не шумела в
    # логах ошибкой на старте.
    try:
        await _client.get_dialogs()
    except Exception as e:
        log.warning("Не удалось обновить список диалогов: %s", e)

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
