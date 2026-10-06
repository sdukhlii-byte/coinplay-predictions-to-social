"""Пересылка zip-китов бота генератору видео (ai-match-lab).

Грабер уже скачивает каждый zip-кит из групп-источников. Если в архиве есть
blank/blank.json (кит бота CoinPlay AI со счетами 10 моделей), копия уходит
POST'ом на VIDEO_GENERATOR_URL — генератор делает по нему видео и кладёт
готовый кит в группу, которую грабер читает как обычно.

Best effort: любая ошибка только пишется в лог и НИКОГДА не мешает обычной
публикации картинок из этого же кита. Свой же видео-кит (без blank/) обратно
не пересылается, так что петли нет.
"""

import logging
import os
import time
import zipfile

import requests

from config import VIDEO_GENERATOR_TOKEN, VIDEO_GENERATOR_URL

log = logging.getLogger("video_bridge")

MARKER = "blank/blank.json"
MAX_BYTES = 25 * 1024 * 1024
RETRY_DELAYS = (2, 6)  # две повторные попытки при сбое сети / 5xx


def is_bot_kit(zip_path: str) -> bool:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return MARKER in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def forward_kit(zip_path: str, name: str = "") -> bool:
    """True — генератор принял архив (в очередь или уже знал его)."""
    if not VIDEO_GENERATOR_URL:
        return False
    if not is_bot_kit(zip_path):
        return False
    size = os.path.getsize(zip_path)
    if size > MAX_BYTES:
        log.warning("кит %s: %d байт — больше лимита %d, генератору не шлю", name, size, MAX_BYTES)
        return False

    with open(zip_path, "rb") as f:
        data = f.read()
    url = f"{VIDEO_GENERATOR_URL}/kit"
    headers = {"Authorization": f"Bearer {VIDEO_GENERATOR_TOKEN}"}
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            r = requests.post(url, params={"name": name or "kit.zip"}, data=data,
                              headers=headers, timeout=30)
        except requests.RequestException as e:
            err = str(e)
        else:
            if r.status_code in (200, 202):
                log.info("кит %s -> генератор: %s", name, r.text[:200])
                return True
            err = f"{r.status_code} {r.text[:200]}"
            if 400 <= r.status_code < 500:  # 401/413/422 — повтор ничего не изменит
                log.warning("кит %s: генератор отклонил (%s)", name, err)
                return False
        if attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])
    log.warning("кит %s: генератор недоступен (%s) — видео не будет", name, err)
    return False
