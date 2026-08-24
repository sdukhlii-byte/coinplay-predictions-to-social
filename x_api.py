"""Клиент X (Twitter) API v2.

Авторизация — OAuth 1.0a user context: постим от имени своего аккаунта,
пользовательский OAuth-флоу не нужен.

Тарификация (2026, pay-per-use): ~$0.015 за пост, но ~$0.20 если в тексте
есть URL. Поэтому прямые ссылки в постах лучше не использовать.
"""

import logging
import os
import re
import time

import requests
from requests_oauthlib import OAuth1

import db
from config import (
    X_ACCESS_SECRET,
    X_ACCESS_TOKEN,
    X_API_KEY,
    X_API_SECRET,
    X_TEXT_LIMIT,
)

log = logging.getLogger("x")

TWEETS_URL = "https://api.x.com/2/tweets"
MEDIA_UPLOAD_URL = "https://upload.x.com/1.1/media/upload.json"
ME_URL = "https://api.x.com/2/users/me"

# Больше 4 вложений X не принимает.
MAX_MEDIA = 4


class XError(RuntimeError):
    pass


def _auth() -> OAuth1:
    return OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET)


def whoami() -> dict:
    r = requests.get(ME_URL, auth=_auth(), timeout=30)
    if r.status_code >= 400:
        raise XError(f"GET users/me -> {r.status_code}: {r.text[:300]}")
    return r.json().get("data", {})


def fit(text: str, limit: int = X_TEXT_LIMIT) -> str:
    """
    Ужимает текст под лимит одного твита.

    Пост состоит из смысловых блоков, разделённых пустой строкой, а последний
    блок — это призыв к действию ("Link in bio"). При разрезании в тред он
    уезжал во второй твит, который почти никто не открывает, поэтому здесь
    последний блок сохраняется всегда, а вырезается середина.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) < 2:
        return text[:limit].rstrip()

    head, tail = blocks[:-1], blocks[-1]

    # Хвост (призыв) сам по себе не влезает — режем жёстко.
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
        log.info("Текст ужат под лимит %d: убрано %d блок(ов) из середины",
                 limit, dropped)
    return result


def split_text(text: str, limit: int = X_TEXT_LIMIT) -> list:
    """Режет текст по границам абзацев/предложений под лимит X."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    hard_limit = limit - 8  # запас под суффикс " (1/3)"
    parts, remaining = [], text

    def find_split(window: str) -> int:
        floor = len(window) // 2
        for sep in ("\n\n", ". ", "! ", "? ", ".\n", "!\n", "?\n", "\n", " "):
            pos = window.rfind(sep)
            if pos >= floor:
                return pos + len(sep) - 1
        return -1

    while len(remaining) > hard_limit:
        window = remaining[:hard_limit]
        split_at = find_split(window)
        if split_at <= 0:
            split_at = hard_limit
        parts.append(remaining[: split_at + 1].strip())
        remaining = remaining[split_at + 1:].strip()

    if remaining:
        parts.append(remaining)

    total = len(parts)
    return [f"{p} ({i + 1}/{total})" for i, p in enumerate(parts)] if total > 1 else parts


def _upload_media(path: str) -> str:
    """Загружает файл и возвращает media_id. X забирает файл напрямую, не по URL."""
    if not os.path.exists(path):
        raise XError(f"Файл не найден: {path}")

    with open(path, "rb") as f:
        r = requests.post(
            MEDIA_UPLOAD_URL,
            auth=_auth(),
            files={"media": f},
            timeout=120,
        )
    if r.status_code >= 400:
        raise XError(f"Загрузка медиа -> {r.status_code}: {r.text[:300]}")

    media_id = r.json().get("media_id_string")
    if not media_id:
        raise XError(f"Ответ без media_id: {r.text[:200]}")
    return media_id


def _create_tweet(text: str, media_ids: list = None, reply_to: str = None) -> str:
    payload = {}
    if text:
        payload["text"] = text
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to}

    r = requests.post(TWEETS_URL, auth=_auth(), json=payload, timeout=60)
    if r.status_code >= 400:
        raise XError(f"POST tweets -> {r.status_code}: {r.text[:400]}")

    return r.json()["data"]["id"]


def publish(text: str, media_entries: list) -> list:
    """
    Публикует пост в X. Длинный текст уходит тредом,
    медиа прикрепляется к первому посту.

    media_entries: [{"kind": ..., "key": ...}] — путь берётся из БД,
    файл загружается напрямую.
    """
    chunks = split_text(text)
    if not chunks and not media_entries:
        raise XError("Нечего публиковать")
    if not chunks:
        chunks = [""]

    media_ids = []
    for entry in media_entries[:MAX_MEDIA]:
        record = db.get_media(entry["key"])
        if not record:
            log.warning("Медиа %s не найдено в базе", entry["key"])
            continue
        media_ids.append(_upload_media(record["path"]))

    published, reply_to = [], None

    for i, chunk in enumerate(chunks):
        ids = media_ids if i == 0 else None
        tweet_id = _create_tweet(chunk, ids, reply_to)
        published.append(tweet_id)
        reply_to = tweet_id
        if i < len(chunks) - 1:
            time.sleep(2)

    return published
