"""Публикация видео в YouTube (Shorts) через YouTube Data API v3.

Видео 9:16 длиной до 3 минут YouTube сам считает Shorts; "#Shorts" в описании
не мешает и подстраховывает.

Авторизация — OAuth refresh-токен канала (получается один раз скриптом
youtube_auth.py). Нужен только scope youtube.upload.

ВАЖНО: пока проект Google Cloud не прошёл аудит YouTube API Services, видео,
загруженные через API, YouTube принудительно делает приватными. Это
ограничение Google, не баг: открыть такое видео можно вручную в YouTube Studio.

Загрузка — resumable upload (INIT -> PUT всего файла одним куском; ролики
у нас 2-8 МБ).
"""

from __future__ import annotations

import json
import logging
import os
import time

import requests

import db
from config import (
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_CLIENT_ID,
    YOUTUBE_CLIENT_SECRET,
    YOUTUBE_PRIVACY,
    YOUTUBE_REFRESH_TOKEN,
)

log = logging.getLogger("youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
TITLE_LIMIT = 100          # лимит YouTube на название
DESCRIPTION_LIMIT = 4900   # лимит 5000 байт, берём с запасом
HTTP_TIMEOUT = 30
UPLOAD_TIMEOUT = 600

_token = {"value": "", "exp": 0.0}


class YouTubeError(RuntimeError):
    pass


def _api_error(r: requests.Response) -> str:
    try:
        err = r.json().get("error", {})
        if isinstance(err, dict):
            reason = ",".join(e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict))
            return f"{r.status_code} {err.get('message', '')} {reason}".strip()
        return f"{r.status_code} {err} {r.json().get('error_description', '')}".strip()
    except ValueError:
        return f"{r.status_code} {r.text[:200]}"


def access_token() -> str:
    """Короткоживущий access-токен из refresh-токена (кешируется до истечения)."""
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    try:
        r = requests.post(TOKEN_URL, data={
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "refresh_token": YOUTUBE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise YouTubeError(f"токен: сеть недоступна ({type(e).__name__})") from None
    if r.status_code != 200:
        raise YouTubeError(f"токен не получен: {_api_error(r)} "
                           "(refresh-токен отозван или просрочен? перевыпусти youtube_auth.py)")
    data = r.json()
    _token["value"] = data["access_token"]
    _token["exp"] = time.time() + int(data.get("expires_in", 3600))
    return _token["value"]


def check() -> None:
    """Проверка при старте: токен вообще выдаётся."""
    access_token()


def split_text(text: str) -> tuple[str, str]:
    """Текст площадки -> (название, описание). Первая строка — название."""
    lines = (text or "").strip().splitlines()
    title = (lines[0] if lines else "").strip()
    description = "\n".join(lines[1:]).strip()
    # YouTube отклоняет < и > в названии/описании
    clean = lambda s: s.replace("<", "").replace(">", "")
    title, description = clean(title), clean(description)
    if len(title) > TITLE_LIMIT:
        title = title[:TITLE_LIMIT - 1].rstrip() + "…"
    if "#shorts" not in (title + description).lower():
        description = (description + "\n\n#Shorts").strip()
    return title or "AI prediction", description[:DESCRIPTION_LIMIT]


def publish(text: str, media_entries: list) -> list:
    """Загружает первое видео из media_entries. Возвращает [video_id]."""
    video = next((m for m in media_entries if m.get("kind") == "video"), None)
    if not video:
        return []
    entry = db.get_media(video["key"])
    path = entry["path"] if entry else ""
    if not path or not os.path.exists(path):
        raise YouTubeError("файл видео не найден на диске")

    title, description = split_text(text)
    size = os.path.getsize(path)
    meta = {
        "snippet": {"title": title, "description": description,
                    "categoryId": YOUTUBE_CATEGORY_ID},
        "status": {"privacyStatus": YOUTUBE_PRIVACY,
                   "selfDeclaredMadeForKids": False},
    }
    headers = {
        "Authorization": f"Bearer {access_token()}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(size),
    }
    try:
        r = requests.post(UPLOAD_URL, params={"uploadType": "resumable",
                                              "part": "snippet,status"},
                          headers=headers, data=json.dumps(meta).encode("utf-8"),
                          timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise YouTubeError(f"старт загрузки: сеть ({type(e).__name__})") from None
    if r.status_code != 200 or not r.headers.get("Location"):
        raise YouTubeError(f"старт загрузки: {_api_error(r)}")

    log.info("YouTube: загружаю %.1f МБ, название %r, доступ %s",
             size / 1024 / 1024, title, YOUTUBE_PRIVACY)
    try:
        with open(path, "rb") as f:
            up = requests.put(r.headers["Location"], data=f,
                              headers={"Content-Type": "video/mp4",
                                       "Content-Length": str(size)},
                              timeout=UPLOAD_TIMEOUT)
    except requests.RequestException as e:
        raise YouTubeError(f"загрузка: сеть ({type(e).__name__})") from None
    if up.status_code not in (200, 201):
        raise YouTubeError(f"загрузка: {_api_error(up)}")
    video_id = up.json().get("id")
    if not video_id:
        raise YouTubeError("YouTube не вернул id видео")
    log.info("YouTube: готово https://youtube.com/shorts/%s", video_id)
    return [video_id]
