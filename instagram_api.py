"""Клиент Instagram Graph API (публикация от Business/Creator аккаунта).

Публикация в Instagram официально возможна только через Graph API и только
для Business/Creator аккаунта, связанного с Facebook-страницей. Личный аккаунт
через API не постит — это ограничение Meta, не обходится кодом.

Токен получен через Facebook Login (FB-страница + Graph API Explorer,
токен вида "EAA..."), поэтому все запросы идут на graph.facebook.com — это
тот же домен и тот же протокол, что и для обычных Facebook API-вызовов.
(Отдельно у Meta есть флоу "Instagram API с прямым Instagram Login" с
токенами вида "IGAA...", который ходит через graph.instagram.com — это
другой продукт, в этом файле не используется.)

Как и Threads, Instagram скачивает медиа сам по URL — файл напрямую не
загружается. Поэтому картинки отдаются с нашего сервиса
(PUBLIC_BASE_URL/media/{key}), ровно как уже сделано для Threads.

Публикация двухшаговая:
  1. создать media container (для одной картинки — сразу, для нескольких —
     контейнер-карусель из дочерних контейнеров);
  2. дождаться статуса FINISHED и вызвать media_publish.

Instagram не поддерживает "тред" и не режет длинный текст — подпись просто
ужимается под лимит (IG_CAPTION_LIMIT).
"""

import logging
import time

import requests

import db
from config import (
    INSTAGRAM_ACCESS_TOKEN,
    INSTAGRAM_CAPTION_LIMIT,
    INSTAGRAM_USER_ID,
    IG_GRAPH,
    META_APP_ID,
    META_APP_SECRET,
)

log = logging.getLogger("instagram")

# Instagram обрабатывает контейнер асинхронно — ждём готовности перед публикацией.
CONTAINER_POLL_ATTEMPTS = 30
CONTAINER_POLL_INTERVAL = 4

# Карусель IG: от 2 до 10 элементов. Одиночная картинка публикуется без карусели.
MAX_CAROUSEL = 10


class InstagramError(RuntimeError):
    pass


def current_token() -> str:
    """Токен из БД (если был продлён), иначе из переменной окружения."""
    return db.get_state("instagram_access_token", INSTAGRAM_ACCESS_TOKEN)


def _post(path: str, payload: dict) -> dict:
    payload = {**payload, "access_token": current_token()}
    r = requests.post(f"{IG_GRAPH}/{path}", data=payload, timeout=60)
    if r.status_code >= 400:
        raise InstagramError(f"POST {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def _get(path: str, params: dict) -> dict:
    params = {**params, "access_token": current_token()}
    r = requests.get(f"{IG_GRAPH}/{path}", params=params, timeout=60)
    if r.status_code >= 400:
        raise InstagramError(f"GET {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def fit_caption(text: str, limit: int = INSTAGRAM_CAPTION_LIMIT) -> str:
    """
    Ужимает подпись под лимит IG. Смысловые блоки разделены пустой строкой,
    последний блок — призыв ("Link in bio"), он сохраняется всегда;
    вырезается середина. Та же логика, что у X.fit.
    """
    import re

    text = (text or "").strip()
    if len(text) <= limit:
        return text

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) < 2:
        return text[:limit].rstrip()

    head, tail = blocks[:-1], blocks[-1]
    if len(tail) >= limit:
        return text[:limit].rstrip()

    kept = []
    for block in head:
        trial = kept + [block]
        if len("\n\n".join(trial + [tail])) > limit:
            break
        kept = trial

    result = "\n\n".join(kept + [tail])
    dropped = len(head) - len(kept)
    if dropped:
        log.info("Подпись ужата под лимит %d: убрано %d блок(ов)", limit, dropped)
    return result


def _wait_container_ready(container_id: str):
    """Ждёт, пока IG дообработает контейнер (видео может занять минуту)."""
    for _ in range(CONTAINER_POLL_ATTEMPTS):
        data = _get(container_id, {"fields": "status_code,status"})
        status = data.get("status_code") or data.get("status")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(f"Контейнер {container_id}: {status}")
        time.sleep(CONTAINER_POLL_INTERVAL)
    raise InstagramError(f"Контейнер {container_id} не готов после ожидания")


def _create_item_container(media: dict) -> str:
    """Дочерний элемент карусели."""
    payload = {"is_carousel_item": "true"}
    if media["kind"] == "video":
        payload["media_type"] = "VIDEO"
        payload["video_url"] = media["url"]
    else:
        payload["image_url"] = media["url"]
    return _post(f"{INSTAGRAM_USER_ID}/media", payload)["id"]


def _create_container(caption: str, media: list) -> str:
    if not media:
        raise InstagramError("Instagram требует хотя бы одно изображение или видео")

    if len(media) == 1:
        item = media[0]
        payload = {}
        if caption:
            payload["caption"] = caption
        if item["kind"] == "video":
            payload["media_type"] = "VIDEO"
            payload["video_url"] = item["url"]
        else:
            payload["image_url"] = item["url"]
        container_id = _post(f"{INSTAGRAM_USER_ID}/media", payload)["id"]
        _wait_container_ready(container_id)
        return container_id

    # Карусель: сначала дочерние контейнеры, ждём готовности каждого,
    # затем родительский CAROUSEL с подписью.
    children = [_create_item_container(m) for m in media[:MAX_CAROUSEL]]
    for child in children:
        _wait_container_ready(child)

    payload = {"media_type": "CAROUSEL", "children": ",".join(children)}
    if caption:
        payload["caption"] = caption
    container_id = _post(f"{INSTAGRAM_USER_ID}/media", payload)["id"]
    _wait_container_ready(container_id)
    return container_id


def publish(caption: str, media: list) -> list:
    """
    Публикует пост в Instagram. media: [{"kind": "image"|"video", "url": ...}].
    Возвращает список из одного id опубликованного поста (тредов у IG нет).
    """
    if not media:
        raise InstagramError("Нечего публиковать: Instagram требует медиа")

    caption = fit_caption(caption)
    container_id = _create_container(caption, media)

    # Пауза между созданием и публикацией — требование API.
    time.sleep(2)
    post_id = _post(f"{INSTAGRAM_USER_ID}/media_publish",
                    {"creation_id": container_id})["id"]

    return [post_id]


def whoami() -> dict:
    return _get(INSTAGRAM_USER_ID, {"fields": "id,username"})


def refresh_token_if_needed(min_days_left: int = 10) -> bool:
    """
    Продлевает long-lived токен, если осталось мало времени.

    Токен получен через Facebook Login (EAA...), поэтому продлевается тем же
    механизмом, что и обычные Facebook-токены — fb_exchange_token на
    graph.facebook.com. Для этого нужны данные приложения (META_APP_ID /
    META_APP_SECRET), в отличие от нативных IG-Login токенов (IGAA...),
    которые продлеваются по одному только текущему токену.
    """
    try:
        info = _get("debug_token", {"input_token": current_token()})
        expires_at = info.get("data", {}).get("expires_at", 0)
    except InstagramError as e:
        log.warning("Не удалось проверить срок токена: %s", e)
        return False

    if not expires_at:
        return False

    days_left = (expires_at - time.time()) / 86400
    if days_left > min_days_left:
        return False

    if not (META_APP_ID and META_APP_SECRET):
        log.warning(
            "IG-токену осталось %.1f дн., но META_APP_ID/META_APP_SECRET не "
            "заданы — автопродление невозможно, обновите токен вручную",
            days_left,
        )
        return False

    log.info("IG-токену осталось %.1f дн., продлеваю", days_left)
    r = requests.get(
        f"{IG_GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "fb_exchange_token": current_token(),
        },
        timeout=60,
    )
    if r.status_code >= 400:
        log.error("Не удалось продлить IG-токен: %s", r.text[:500])
        return False

    new_token = r.json().get("access_token")
    if new_token:
        db.set_state("instagram_access_token", new_token)
        log.info("IG-токен продлён")
        return True
    return False
