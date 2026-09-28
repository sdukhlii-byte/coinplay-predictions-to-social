"""Клиент Threads Graph API."""

import logging
import time

import requests

import db
from config import (
    THREADS_ACCESS_TOKEN,
    THREADS_GRAPH,
    THREADS_TEXT_LIMIT,
    THREADS_USER_ID,
)

log = logging.getLogger("threads")

# Threads обрабатывает контейнер асинхронно (особенно видео) — ждём готовности.
CONTAINER_POLL_ATTEMPTS = 30
CONTAINER_POLL_INTERVAL = 4


class ThreadsError(RuntimeError):
    pass


def current_token() -> str:
    """Токен из БД (если был обновлён), иначе из переменной окружения."""
    return db.get_state("threads_access_token", THREADS_ACCESS_TOKEN)


def _post(path: str, payload: dict) -> dict:
    payload = {**payload, "access_token": current_token()}
    r = requests.post(f"{THREADS_GRAPH}/{path}", data=payload, timeout=60)
    if r.status_code >= 400:
        raise ThreadsError(f"POST {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def _get(path: str, params: dict) -> dict:
    params = {**params, "access_token": current_token()}
    r = requests.get(f"{THREADS_GRAPH}/{path}", params=params, timeout=60)
    if r.status_code >= 400:
        raise ThreadsError(f"GET {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def split_text(text: str, limit: int = THREADS_TEXT_LIMIT) -> list:
    """
    Делит текст по границам абзацев/предложений. Смысл не переписывается,
    длинный пост становится связанным тредом.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    hard_limit = limit - 8  # запас под суффикс " (1/2)"
    parts, remaining = [], text

    # Приоритет границ: сначала абзац, потом конец предложения, потом перенос,
    # и только в крайнем случае пробел. Брать max() по всем нельзя — пробел
    # почти всегда окажется ближе к концу окна и перебьёт границу предложения.
    def _find_split(window: str) -> int:
        # Режем не раньше половины окна, иначе части выходят слишком рваными.
        floor = len(window) // 2
        for sep in ("\n\n", ". ", "! ", "? ", ".\n", "!\n", "?\n", "\n", " "):
            pos = window.rfind(sep)
            if pos >= floor:
                return pos + len(sep) - 1
        return -1

    while len(remaining) > hard_limit:
        window = remaining[:hard_limit]
        split_at = _find_split(window)
        if split_at <= 0:
            split_at = hard_limit
        parts.append(remaining[: split_at + 1].strip())
        remaining = remaining[split_at + 1:].strip()

    if remaining:
        parts.append(remaining)

    total = len(parts)
    return [f"{p} ({i + 1}/{total})" for i, p in enumerate(parts)] if total > 1 else parts


def _wait_container_ready(container_id: str):
    """Ждёт, пока Threads дообработает контейнер (видео может занять минуту)."""
    for _ in range(CONTAINER_POLL_ATTEMPTS):
        data = _get(container_id, {"fields": "status,error_message"})
        status = data.get("status")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise ThreadsError(f"Контейнер {container_id}: {status} {data.get('error_message', '')}")
        time.sleep(CONTAINER_POLL_INTERVAL)
    raise ThreadsError(f"Контейнер {container_id} не готов после ожидания")


def _create_item_container(media: dict) -> str:
    """Элемент карусели."""
    payload = {"is_carousel_item": "true"}
    if media["kind"] == "video":
        payload["media_type"] = "VIDEO"
        payload["video_url"] = media["url"]
    else:
        payload["media_type"] = "IMAGE"
        payload["image_url"] = media["url"]
    return _post(f"{THREADS_USER_ID}/threads", payload)["id"]


def _create_container(text: str, media: list, reply_to_id: str = None) -> str:
    payload = {}
    if text:
        payload["text"] = text
    if reply_to_id:
        payload["reply_to_id"] = reply_to_id

    if not media:
        payload["media_type"] = "TEXT"
    elif len(media) == 1:
        item = media[0]
        if item["kind"] == "video":
            payload["media_type"] = "VIDEO"
            payload["video_url"] = item["url"]
        else:
            payload["media_type"] = "IMAGE"
            payload["image_url"] = item["url"]
    else:
        children = [_create_item_container(m) for m in media[:20]]
        for child in children:
            _wait_container_ready(child)
        payload["media_type"] = "CAROUSEL"
        payload["children"] = ",".join(children)

    container_id = _post(f"{THREADS_USER_ID}/threads", payload)["id"]
    if media:
        _wait_container_ready(container_id)
    return container_id


def publish(text: str, media: list) -> list:
    """
    Публикует пост. Длинный текст уходит связанным тредом,
    медиа прикрепляется к первому посту треда.

    media: [{"kind": "image"|"video", "url": "https://..."}]
    Возвращает список id опубликованных постов.
    """
    chunks = split_text(text)
    if not chunks and not media:
        raise ThreadsError("Нечего публиковать: ни текста, ни медиа")
    if not chunks:
        chunks = [""]

    published, reply_to = [], None

    for i, chunk in enumerate(chunks):
        chunk_media = media if i == 0 else []
        container_id = _create_container(chunk, chunk_media, reply_to)

        # Небольшая пауза между созданием и публикацией — требование API.
        time.sleep(2)
        post_id = _post(f"{THREADS_USER_ID}/threads_publish",
                        {"creation_id": container_id})["id"]

        published.append(post_id)
        reply_to = post_id
        if i < len(chunks) - 1:
            time.sleep(2)

    return published


def whoami() -> dict:
    return _get("me", {"fields": "id,username"})


def get_insights(media_id: str) -> dict:
    """
    Просмотры и лайки одного поста. Threads insights отдаёт метрики
    "total_value" (лайфтайм-метрика), а не таймсерию — но на всякий случай
    подстраховываемся под оба возможных формата ответа.
    """
    data = _get(f"{media_id}/insights", {"metric": "views,likes"})
    out = {"views": 0, "likes": 0}
    for item in data.get("data", []):
        name = item.get("name")
        if name not in out:
            continue
        total = item.get("total_value") or {}
        if "value" in total:
            out[name] = total["value"]
        elif item.get("values"):
            out[name] = item["values"][0].get("value", 0)
    return out


def refresh_token_if_needed(min_days_left: int = 10) -> bool:
    """
    Продлевает long-lived токен, если до истечения осталось мало времени.
    Работает только с токеном, которому больше 24 часов.
    """
    try:
        info = _get("debug_token", {"input_token": current_token()})
        expires_at = info.get("data", {}).get("expires_at", 0)
    except ThreadsError as e:
        log.warning("Не удалось проверить срок токена: %s", e)
        return False

    if not expires_at:
        return False

    days_left = (expires_at - time.time()) / 86400
    if days_left > min_days_left:
        return False

    log.info("Токену осталось %.1f дн., продлеваю", days_left)
    r = requests.get(
        "https://graph.threads.net/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": current_token()},
        timeout=60,
    )
    if r.status_code >= 400:
        log.error("Не удалось продлить токен: %s", r.text[:500])
        return False

    new_token = r.json().get("access_token")
    if new_token:
        db.set_state("threads_access_token", new_token)
        log.info("Токен продлён")
        return True
    return False
