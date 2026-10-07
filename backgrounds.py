"""Генерация тематического фона под КОНКРЕТНЫЙ матч: рука на фоне стадиона/
арены/октагона, а толпа/баннеры на фоне — в цветах и с флагами команд этого
матча (как на референсе: Бельгия -> бельгийские флаги на трибунах), а не
один статичный файл на всю вертикаль.

Рука в кадре НИЧЕГО не держит — ни бумаги, ни карточки (см. build_prompt):
реальный распечатанный лист (poster.render_paper) накладывается уже потом,
в poster.compose_frame(). Раньше промпт просил сгенерировать ещё и саму
бумагу в руке — на практике это давало на итоговом кадре ДВА листа сразу:
нарисованный ИИ (не совпадающий по размеру/положению) и наш настоящий
поверх него (см. жалобу "бумажку снизу, а то их две выходит").

Платный шаг (вызов OpenAI Images API, модель gpt-image-1) — выключен по
умолчанию, включается явно переменной AI_BACKGROUND=1 (см. generate.py и
README). Раньше был на Gemini (gemini-2.5-flash-image) — сменили по
явному запросу на OpenAI; кеширование не менялось, поменялся сам вызов API
и (отдельно) промпт сцены. Фон кешируется в out/<slug>/_bg.jpg, повторные
прогоны того же матча его не перегенерируют.
"""

from __future__ import annotations

import base64
import io
import logging
import os

import requests
from PIL import Image

log = logging.getLogger("aml.bg")

MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
API_URL = "https://api.openai.com/v1/images/generations"
# Портретные размеры, которые реально поддерживает gpt-image-1 — ровно 9:16
# среди них нет, берём самый узкий/высокий (1024x1536, ~2:3): compose_frame()
# всё равно масштабирует и обрезает фон под FRAME_W/FRAME_H по центру (см.
# poster.compose_frame), так что лишние поля по бокам после обрезки не видны.
SIZE = os.environ.get("OPENAI_IMAGE_SIZE", "1024x1536")

COMMON = (
    "A realistic, authentic photo taken at a real sports venue, vertical "
    "9:16 orientation — reads as a genuine photograph of the actual place, "
    "natural ambient lighting, accurate colors, ordinary realistic exposure, "
    "not an artificial render and not an exaggerated cinematic color grade. "
    "Behind everything, {scene}. "
    "One human hand enters from the lower part of the frame, fingers curled "
    "upward in sharp focus as if gripping and holding up a flat object in "
    "front of the camera, centered in the lower two-thirds of the frame — "
    "but the hand is holding NOTHING VISIBLE: no paper, no card, no sign, no "
    "phone, no object of any kind, just the empty gripping gesture. This "
    "matters because a separate real printed sheet gets composited into "
    "that empty space afterward, so the area right in front of the fingers "
    "must stay plain, uncluttered background — no important detail, text or "
    "object placed there, so the later composite reads as one coherent "
    "photo instead of two overlapping pictures. The rest of the background "
    "stays a believable, true-to-life location, softly out of focus right "
    "around the hand (ordinary camera depth of field) so background text/ "
    "logos only need to read as colors and shapes, not be legible. "
    "No other hands, no arms besides the one, no faces or eyes in sharp "
    "focus, no watermarks, no real brand or competition logos."
)

NEGATIVE = (
    " Avoid: any paper, card, sign, phone, book or other object visibly "
    "held in or resting against the hand, a second hand, cartoonish or "
    "illustrated style, airbrushed or CGI-smooth plastic-looking surfaces, "
    "stock-photo or advertisement look, overly dramatic or moody cinematic "
    "color grading, extreme close-up that crops the hand out of frame, "
    "sharply legible background text."
)

# {teams} — вставляется в сцену ниже; для generate_backgrounds.py (статичные
# превью без привязки к матчу) teams-предложение просто не подставляется.
SCENES = {
    "football": (
        "a packed football/soccer stadium at night — green pitch, white "
        "lines, bright floodlights glowing{teams}"
    ),
    "esports": (
        "a dark esports arena stage — big blurred screens, colorful RGB "
        "stage lighting (blue, purple, magenta), crowd silhouettes{teams}"
    ),
    "ufc": (
        "a blurred octagon cage fence and arena lights, dramatic red and "
        "white spotlights, crowd silhouettes{teams}"
    ),
}


def _teams_clause(vertical: str, home: str, away: str) -> str:
    if not home or not away:
        return ""
    if vertical == "ufc":
        return (
            f', corner banners and arena signage hinting at "{home}" in one '
            f'corner and "{away}" in the other, each corner lit in a '
            f"different accent color"
        )
    return (
        f", the crowd in the stands split into two colors — fans, flags and "
        f'banners for "{home}" filling one side and for "{away}" filling '
        f"the other side"
    )


def build_prompt(vertical: str, home: str = "", away: str = "") -> str:
    scene = SCENES[vertical].format(teams=_teams_clause(vertical, home, away))
    return COMMON.format(scene=scene) + NEGATIVE


def call_openai(api_key: str, prompt: str, timeout: int = 90) -> bytes:
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": MODEL, "prompt": prompt, "size": SIZE, "n": 1},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"Пустой ответ (возможно модерация промпта): {data}")
    b64 = items[0].get("b64_json")
    if not b64:
        raise RuntimeError(f"В ответе нет картинки: {data}")
    return base64.b64decode(b64)


def generate_to_file(vertical: str, home: str, away: str, out_path: str,
                      api_key: str = "") -> bool:
    """True — сгенерировал и сохранил фон под этот матч в out_path.
    False — не вышло (нет ключа, сеть, модерация и т.п.); вызывающий код
    должен в этом случае просто продолжить со старым TABLE_IMAGE/процедурным
    фоном — генерация фона никогда не должна ронять весь прогон."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        log.warning("AI_BACKGROUND включён, но OPENAI_API_KEY не задан")
        return False
    try:
        raw = call_openai(api_key, build_prompt(vertical, home, away))
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.save(out_path, "JPEG", quality=92)
        log.info("Фон под %s vs %s -> %s", home, away, out_path)
        return True
    except Exception as e:  # noqa: BLE001 — фон не критичен, не роняем прогон
        log.warning("Фон для %s vs %s не сгенерился (%s) — беру запасной",
                    home, away, e)
        return False
