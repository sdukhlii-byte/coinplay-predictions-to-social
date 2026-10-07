"""Видео «счёт появляется на табло» (first-frame + last-frame).

Почему два кадра, а не один: text-to-video / image-to-video модель сама
не напишет нужные цифры — она выдумает свои. Поэтому мы отдаём ей
первый кадр (пустой бланк) и последний (бланк с нашими цифрами), а модель
придумывает только то, КАК одно превращается в другое. Итоговые цифры
гарантированно совпадают с прогнозами и подписью поста.

Сценарий один для всех матчей: простой распечатанный листок в руке на
тематическом фоне (стадион/арена/октагон — см. compose_frame в poster.py),
один бокс со счётом, который на последнем кадре уже заполнен нашими
цифрами. VIDEO_STYLE из переменных окружения больше не влияет на промпт
(оставлен no-op ради обратной совместимости).

Провайдеры (VIDEO_PROVIDER) — два разных API, оба поддерживают first-frame +
last-frame (нужно и там, и там: см. выше почему):

  Через OpenRouter (openrouter.ai/docs/guides/overview/multimodal/video-generation),
  тот же OPENROUTER_API_KEY, что уже используется для прогнозов в predictions.py:
    or-seedance-fast  (по умолчанию) — bytedance/seedance-2.0-fast, ~$0.04/сек.
                       Раньше по умолчанию стоял or-veo31lite — самый дешёвый
                       вариант, но реальные генерации показали, что он плохо
                       держит мелкий текст и рамки: текст плывёт уже на первом
                       кадре, проценты расползаются по всему экрану, клетка со
                       счётом иногда превращается в сплошную заливку без цифры.
                       Сменили на seedance-fast — сопоставимо по цене, качество
                       на этом сценарии ещё предстоит проверить;
    or-veo31lite      — google/veo-3.1-lite, $0.03/сек без звука на 720p — на
                       8-секундный сегмент это ~$0.24, тот же "почерк" Google
                       Veo, что и дорогой veo31, но кратно дешевле и, на
                       практике, кратно менее точный с мелким текстом;
    or-seedance-mini  — bytedance/seedance-2.0-mini, ~$0.034/сек, ещё дешевле,
                       но модель меньше — качество может просесть.

  Через fal.ai (нужен отдельный FAL_KEY):
    kling25 — fal-ai/kling-video/v2.5-turbo/pro/image-to-video: ~$0.70 за
              10-секундный сегмент, без звука;
    veo31   — fal-ai/veo3.1/first-last-frame-to-video: самый фотореалистичный,
              8 сек, 9:16, со звуком, но $0.40/сек — на SEGMENTS=2 это ~$6.4.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import random
import shutil
import subprocess
import time
import wave

import numpy as np
import requests
from PIL import Image

from config import env_bool, env_float, env_int, env_str, require_env

log = logging.getLogger("video")

QUEUE = "https://queue.fal.run"
OR_API = "https://openrouter.ai/api/v1"

# fal.ai — нужен FAL_KEY
PROVIDERS = {
    "veo31": "fal-ai/veo3.1/first-last-frame-to-video",
    "kling25": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
}

# OpenRouter — нужен OPENROUTER_API_KEY (тот же, что и для прогнозов)
OPENROUTER_MODELS = {
    "or-veo31lite": "google/veo-3.1-lite",
    "or-seedance-fast": "bytedance/seedance-2.0-fast",
    "or-seedance-mini": "bytedance/seedance-2.0-mini",
}

# fal у Kling режет prompt/negative_prompt на 2500 символов — держим с запасом,
# особенно NEGATIVE (он не зависит от числа строк, а prompt растёт с ними).

# Один хват, один лист, один бокс со счётом — ничего больше в кадре не
# меняется. Раньше тут был выбор между «ИИ-устройством» и «рукой с
# маркером» (VIDEO_STYLE=cyber/marker); оба отменены в пользу простого
# распечатанного листка в руке на тематическом фоне (см. референс Stan'а).
# style()/VIDEO_STYLE оставлены как no-op для обратной совместимости
# переменных окружения, но на промпт больше не влияют.
NEGATIVE_SHEET = (
    "second hand, extra hand, another person, extra fingers, deformed or distorted hand, "
    "hand changing grip or position, hand moving, sheet moving, sheet tilting or rotating, "
    "sheet being put down or picked up, camera movement, zoom, pan, shake, "
    "pen, pencil, marker, brush, ink smears, smudges, streaks, handwriting motion, "
    "text changes anywhere except the score box, distorted letters, extra boxes, layout "
    "changes, watermark, ghost or duplicate digits, digits appearing outside the score box, "
    "digits overflowing the box edges, oversized digits, warped or melting numerals, "
    "unreadable numerals, flicker across the whole frame, idle pauses, dead time, action "
    "stopping before the video ends, already-filled box blanking out and refilling, digit "
    "disappearing then reappearing, loading or scramble animation repeating after the digit "
    "already shows, logo or flag badges changing or glowing, background crowd suddenly "
    "sharp or changing, lens flare, sparks, particle burst, confetti, fireworks, extra props "
    "appearing or vanishing, duplicated or doubled caption text at the bottom of the frame, "
    "two overlapping copies of the same word at different sizes, letters from two different "
    "words merging together, caption text flickering between two different phrases, garbled "
    "or scrambled bottom caption")


def style() -> str:
    """Оставлено для обратной совместимости VIDEO_STYLE в окружении — промпт
    больше не зависит от значения, всегда один сценарий «лист в руке»."""
    s = env_str("VIDEO_STYLE", "cyber").lower()
    return s if s in ("cyber", "marker") else "cyber"


def negative() -> str:
    return NEGATIVE_SHEET


class VideoError(RuntimeError):
    pass


# ------------------------------------------------------------- требования ---

def ensure_tools() -> None:
    """Без ffmpeg склейка падает с FileNotFoundError из глубины subprocess —
    проверяем заранее и один раз, с внятным текстом."""
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise VideoError(
            f"Не найдены {', '.join(missing)} — установи ffmpeg "
            "(в Docker-образе это уже сделано; локально: apt install ffmpeg / brew install ffmpeg) "
            "или запусти с --no-video.")


# ---------------------------------------------------------------- промпт ---

def build_prompt(vertical: str, home_val, away_val,
                  home_name: str = "", away_name: str = "") -> str:
    """Промпт сегмента: лист держат в руке на тематическом фоне, главный бокс
    заполняется — ничего больше в кадре не меняется.

    `home_val`/`away_val` — то же самое значение, что уже нарисовано в
    keyframe'ах (poster.Match.hero_home/hero_away — консенсус/большинство,
    см. generate.run()), а НЕ сырой прогноз первой по порядку модели: текст
    промпта обязан описывать те же цифры, что реально стоят в last-frame,
    иначе видео-модель может попытаться "исправить" картинку под текст.

    UFC особый случай — счёта нет, в боксе появляется имя победителя (см.
    poster._score_box), поэтому и тут описываем появление ИМЕНИ, а не цифр.

    Пробовали синхронно с боксом раскрывать ещё и полоску моделей/строку
    консенсуса (интрига как на референсе) — на реальной генерации видео-
    модель не справлялась с несколькими одновременно меняющимися текстовыми
    зонами и давала "плывущий"/задвоенный текст на переходных кадрах.
    Вернулись к одной анимируемой зоне — только главный бокс; всё остальное
    описываем как неизменное, это заметно надёжнее.
    """
    is_ufc = vertical == "ufc"
    if is_ufc and home_val is not None and away_val is not None:
        winner = home_name if home_val > away_val else away_name
        reveal = (
            f'The only change in the whole clip: the single predicted-winner box on the '
            f'sheet reads empty at the start, then the name "{winner}" materializes cleanly '
            "into the box, centered, never sliding in from outside the box, never flickering "
            "or re-appearing once shown, never appearing anywhere else on the sheet."
        )
    else:
        home = home_val if home_val is not None else ""
        away = away_val if away_val is not None else ""
        reveal = (
            f'The only change in the whole clip: the single score box on the sheet reads '
            f'empty at the start, then the digits "{home}" and "{away}" materialize cleanly '
            "into their box side by side, centered, never sliding in from outside the box, "
            "never flickering or re-appearing once shown, never appearing anywhere else on "
            "the sheet."
        )
    return (
        "Locked-off handheld photo/video: one hand holds up a single printed white paper "
        "scorecard at a live sports venue — crowd, lights and the event visible but softly "
        "out of focus behind the sheet. The hand, the sheet and the venue stay completely "
        "still for the whole clip: no camera movement, no zoom, no hand repositioning, no "
        "second hand, no pen or marker ever enters frame.\n"
        f"{reveal}\n"
        "Everything else on the sheet — the small logo, the brand name, the team names, the "
        "flag badges, any small model-icon row, the divider lines, the bottom "
        "call-to-action text — stays perfectly sharp and unchanged throughout. No idle "
        "pause, no dead time: the reveal happens smoothly over the clip and finishes "
        "exactly when it ends."
    )


# ------------------------------------------------------------------ fal ----

def _data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _headers() -> dict:
    key = require_env("FAL_KEY", "Ключ fal.ai нужен для рендера видео. "
                                 "Без него запускай с --no-video.")
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def _get_json(url: str, timeout: int) -> dict:
    r = requests.get(url, headers=_headers(), timeout=timeout)
    if r.status_code >= 400:
        raise VideoError(f"fal {url} -> {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError as e:
        raise VideoError(f"fal {url}: ответ не JSON: {r.text[:200]}") from e


def _run(endpoint: str, payload: dict, timeout: int | None = None) -> dict:
    timeout = timeout or env_int("FAL_TIMEOUT_SEC", 900, lo=60, hi=3600)
    r = requests.post(f"{QUEUE}/{endpoint}", headers=_headers(), json=payload, timeout=120)
    if r.status_code >= 400:
        raise VideoError(f"fal submit {endpoint} -> {r.status_code}: {r.text[:500]}")
    try:
        job = r.json()
    except ValueError as e:
        raise VideoError(f"fal submit {endpoint}: ответ не JSON: {r.text[:200]}") from e

    request_id = job.get("request_id")
    status_url = job.get("status_url")
    response_url = job.get("response_url")
    if not (status_url and response_url):
        if not request_id:
            raise VideoError(f"fal: в ответе нет ни request_id, ни ссылок: {str(job)[:300]}")
        status_url = status_url or f"{QUEUE}/{endpoint}/requests/{request_id}/status"
        response_url = response_url or f"{QUEUE}/{endpoint}/requests/{request_id}"
    log.info("fal: задача %s поставлена (%s)", request_id, endpoint)

    deadline = time.time() + timeout
    delay, last_status = 3.0, ""
    while time.time() < deadline:
        time.sleep(delay)
        delay = min(delay * 1.3, 15.0)  # мягкий backoff: не долбим статус каждые 6 сек час подряд
        try:
            s = _get_json(status_url, timeout=60)
        except (VideoError, requests.RequestException) as e:
            log.warning("fal: статус недоступен (%s) — повторю", e)
            continue
        st = (s.get("status") or "").upper()
        if st != last_status:
            log.info("fal: %s", st or "?")
            last_status = st
        if st == "COMPLETED":
            break
        if st in ("FAILED", "ERROR", "CANCELLED"):
            raise VideoError(f"fal: задача упала: {str(s)[:400]}")
    else:
        raise VideoError(f"fal: не дождался результата за {timeout} сек (request_id={request_id})")

    return _get_json(response_url, timeout=120)


def _video_url(result: dict) -> str:
    """У разных эндпоинтов fal результат лежит то в `video`, то в `videos[0]`."""
    node = result.get("video")
    if isinstance(node, dict) and node.get("url"):
        return node["url"]
    if isinstance(node, str) and node:
        return node
    for item in result.get("videos") or []:
        if isinstance(item, dict) and item.get("url"):
            return item["url"]
        if isinstance(item, str) and item:
            return item
    raise VideoError(f"fal: в ответе нет видео: {str(result)[:300]}")


def _download(url: str, out_path: str, headers: dict | None = None) -> None:
    tmp = f"{out_path}.part"
    with requests.get(url, headers=headers, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
    if os.path.getsize(tmp) < 10_000:  # пустышка вместо ролика — лучше узнать сразу
        os.remove(tmp)
        raise VideoError(f"скачанный файл подозрительно мал ({url})")
    os.replace(tmp, out_path)


# ------------------------------------------------------------ openrouter ---

def _or_headers() -> dict:
    key = require_env(
        "OPENROUTER_API_KEY",
        "Ключ OpenRouter нужен и для видео (or-*), и для прогнозов моделей — "
        "один и тот же ключ.")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": env_str("OPENROUTER_REFERER", "https://t.me/aimatchlab"),
        "X-Title": "AI Match Lab",
    }


def _or_frame(img: Image.Image, frame_type: str) -> dict:
    return {"type": "image_url", "image_url": {"url": _data_uri(img)}, "frame_type": frame_type}


def _or_submit(model: str, prompt: str, first: Image.Image, last: Image.Image) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        # По умолчанию SEGMENTS даёт 1 строку (2 клетки) на сегмент — 4 сек с
        # запасом хватает руке физически дойти и коснуться обеих клеток; для
        # veo-3.1-lite это ещё и минимально короткая из поддерживаемых (4/6/8).
        "duration": env_int("OR_VIDEO_DURATION", 4, lo=1, hi=15),
        "resolution": env_str("OR_VIDEO_RESOLUTION", "720p"),
        "aspect_ratio": "9:16",
        "generate_audio": env_bool("OR_VIDEO_AUDIO", False),
        "frame_images": [_or_frame(first, "first_frame"), _or_frame(last, "last_frame")],
    }
    r = requests.post(f"{OR_API}/videos", headers=_or_headers(), json=payload, timeout=120)
    if r.status_code >= 400:
        raise VideoError(f"openrouter submit {model} -> {r.status_code}: {r.text[:500]}")
    try:
        job = r.json()
    except ValueError as e:
        raise VideoError(f"openrouter submit {model}: ответ не JSON: {r.text[:200]}") from e
    job_id = job.get("id")
    if not job_id:
        raise VideoError(f"openrouter: в ответе нет id задачи: {str(job)[:300]}")
    log.info("openrouter: задача %s поставлена (%s)", job_id, model)
    return job_id


def _or_poll(job_id: str, timeout: int | None = None) -> dict:
    timeout = timeout or env_int("OR_VIDEO_TIMEOUT_SEC", 900, lo=60, hi=3600)
    url = f"{OR_API}/videos/{job_id}"
    deadline = time.time() + timeout
    delay, last_status = 3.0, ""
    while time.time() < deadline:
        time.sleep(delay)
        delay = min(delay * 1.3, 15.0)  # мягкий backoff, как у fal-луп ниже
        try:
            r = requests.get(url, headers=_or_headers(), timeout=60)
            r.raise_for_status()
            s = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("openrouter: статус недоступен (%s) — повторю", e)
            continue
        st = (s.get("status") or "").lower()
        if st != last_status:
            log.info("openrouter: %s", st or "?")
            last_status = st
        if st == "completed":
            cost = (s.get("usage") or {}).get("cost")
            if cost is not None:
                log.info("openrouter: сегмент стоил $%s", cost)
            return s
        if st in ("failed", "cancelled", "expired"):
            raise VideoError(f"openrouter: задача {st}: {str(s)[:400]}")
    raise VideoError(f"openrouter: не дождался результата за {timeout} сек (id={job_id})")


def _or_video_url(result: dict) -> str:
    urls = result.get("unsigned_urls") or []
    if urls and urls[0]:
        return urls[0]
    raise VideoError(f"openrouter: в ответе нет unsigned_urls: {str(result)[:300]}")


def _payload(provider: str, first: Image.Image, last: Image.Image, prompt: str) -> dict:
    if provider == "veo31":
        return {
            "prompt": prompt,
            "first_frame_url": _data_uri(first),
            "last_frame_url": _data_uri(last),
            "duration": env_str("VEO_DURATION", "8s"),
            "aspect_ratio": "9:16",
            "resolution": env_str("VEO_RESOLUTION", "1080p"),
            "generate_audio": env_bool("VEO_AUDIO", True),
            "negative_prompt": negative(),
        }
    return {
        "prompt": prompt,
        "image_url": _data_uri(first),
        "tail_image_url": _data_uri(last),
        "duration": env_str("KLING_DURATION", "10"),
        "negative_prompt": negative(),
        # Пробовали поднять до 0.8 — стало хуже: модель агрессивнее "подгоняет"
        # кадры под последний референс (уже полностью заполненный бланк) и
        # цифры начинают появляться в клетках РАНЬШЕ, чем маркер до них
        # долистал ("прыгает по клеткам"), вместо честного покадрового письма.
        # Вернули дефолт на 0.6 — это, а не рост cfg, снижает "отсебятину".
        "cfg_scale": env_float("KLING_CFG", 0.6, lo=0.0, hi=1.0),
    }


def generate_segment(first: Image.Image, last: Image.Image, prompt: str, out_path: str) -> str:
    provider = env_str("VIDEO_PROVIDER", "or-seedance-fast").lower()
    if provider in OPENROUTER_MODELS:
        model = OPENROUTER_MODELS[provider]

        def _once():
            job_id = _or_submit(model, prompt, first, last)
            result = _or_poll(job_id)
            # "unsigned_urls" — обманчивое название: это не публичная presigned-
            # ссылка (как у fal), а собственный content-эндпоинт OpenRouter,
            # ему всё равно нужен тот же Bearer-токен, что и на submit/poll —
            # без заголовка отдаёт 401, и сегмент проваливается уже ПОСЛЕ того,
            # как OpenRouter списал деньги за генерацию.
            _download(_or_video_url(result), out_path, headers=_or_headers())
    elif provider in PROVIDERS:
        endpoint = PROVIDERS[provider]
        payload = _payload(provider, first, last, prompt)

        def _once():
            result = _run(endpoint, payload)
            _download(_video_url(result), out_path)
    else:
        known = sorted(OPENROUTER_MODELS) + sorted(PROVIDERS)
        raise VideoError(f"VIDEO_PROVIDER={provider!r} — известны только {', '.join(known)}")

    attempts = env_int("FAL_ATTEMPTS", 2, lo=1, hi=5)
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _once()
            log.info("Сегмент сохранён: %s", out_path)
            return out_path
        except (VideoError, requests.RequestException) as e:
            last_err = e
            log.warning("видео: попытка %d/%d не удалась — %s", attempt, attempts, e)
            if attempt < attempts:
                time.sleep(5 * attempt + random.uniform(0, 2))
    raise VideoError(f"Не удалось сгенерировать сегмент за {attempts} попыт(ки): {last_err}")


# ------------------------------------------------------------- ffmpeg ------

def _ff(*args) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # раньше здесь был check=True, и настоящая причина (сообщение ffmpeg)
        # просто терялась — в логе оставался только код возврата
        raise VideoError(f"ffmpeg завершился с кодом {proc.returncode}:\n"
                         f"{(proc.stderr or '').strip()[:800]}")


def _has_audio(path: str) -> bool:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                           "-show_entries", "stream=index", "-of", "csv=p=0", path],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        log.warning("ffprobe не смог прочитать %s — считаю, что звука нет", path)
        return False
    return bool(proc.stdout.strip())


def _normalize(src: str, dst: str) -> None:
    """Один формат для склейки: 1080x1920, 30 fps, H.264 + AAC (тишина, если звука нет)."""
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p"
    common = ["-c:v", "libx264", "-preset", "medium", "-crf", "19",
              "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
              "-video_track_timescale", "90000"]  # одинаковый timebase — иначе concat рассинхронит звук
    if _has_audio(src):
        _ff("-i", src, "-vf", vf, "-map", "0:v:0", "-map", "0:a:0", *common, dst)
    else:
        _ff("-i", src, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", "-shortest", *common, dst)


def pick_music(seed: str = "") -> str:
    """Какой трек подложить под ролик.

    `MUSIC_FILE` — явный путь к одному треку (как раньше, в приоритете).
    `MUSIC_DIR` — папка с НЕСКОЛЬКИМИ треками (на каждый уже должна быть
    коммерческая лицензия, см. README) — тогда на каждый прогон берётся
    один из них автоматически, и ролики не превращаются в клоны друг
    друга под один и тот же трек. `seed` (обычно id матча) делает выбор
    воспроизводимым при повторном прогоне ТОГО ЖЕ матча, но разным между
    разными матчами.

    Специально НЕТ варианта «тянуть трендовую музыку по API»: у «трендовых»
    звуков TikTok/Reels/Shorts нет публичного API для скачивания и
    переиспользования вне самой площадки — это лицензия только на показ
    внутри неё, и наложение такого звука в сторонний телеграм-ролик — прямое
    нарушение авторских прав (и площадки, и правообладателя трека), а для
    бренда казино — куда более серьёзный риск (жалоба, бан аккаунта, иск),
    чем выигрыш от одного "трендового" звука. Бесплатные библиотеки музыки
    (Jamendo и похожие) тоже не подходят: их бесплатный доступ по API
    явно ограничен НЕкоммерческим использованием, а промо-ролик казино —
    коммерческое использование по их же определению (см. их API Terms of
    Use). Единственный легальный автоматический путь — один раз купить
    небольшую библиотеку треков с коммерческой лицензией и положить их в
    `MUSIC_DIR`: именно их этот хелпер и ротирует, без доплаты за каждый
    ролик."""
    explicit = env_str("MUSIC_FILE")
    if explicit:
        return explicit
    music_dir = env_str("MUSIC_DIR")
    if not music_dir or not os.path.isdir(music_dir):
        return ""
    exts = (".mp3", ".wav", ".m4a", ".aac", ".ogg")
    tracks = sorted(
        os.path.join(music_dir, f) for f in os.listdir(music_dir)
        if f.lower().endswith(exts)
    )
    if not tracks:
        return ""
    rnd = random.Random(seed) if seed else random
    return rnd.choice(tracks)


def _stinger_wav(out_path: str) -> None:
    """Короткий синтетический «дзынь» — два чистых тона с быстрым
    экспоненциальным затуханием, сами сочиняем и рендерим через numpy
    (никаких сторонних сэмплов и, соответственно, лицензий), чтобы отметить
    звуком момент, когда ролик уходит в финальный стоп-кадр с ответом.
    Бесплатно (чистый DSP), не требует никаких платных API."""
    sr = 44100
    dur = 0.55
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    env = np.exp(-6.0 * t)
    tone = (
        0.5 * np.sin(2 * np.pi * 880.0 * t)
        + 0.35 * np.sin(2 * np.pi * 1318.5 * t)  # малая терция вверх — «бодрый» дзынь
    ) * env
    tone = np.clip(tone, -1.0, 1.0)
    pcm = (tone * 32767).astype(np.int16)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _hold_clip(still: str, stinger: str, hold_sec: float, out_path: str) -> None:
    """Стоп-кадр в конце — не плоская заморозка последнего кадра, а медленный
    наезд камеры (Ken Burns) под синтетический звуковой акцент: оба эффекта
    бесплатны (чистый ffmpeg + сгенерированный wav), но вместе ощутимо
    поднимают «продакшен» именно в точке, где зритель читает ответ.
    Центрированный зум (x/y-выражения), чтобы бокс со счётом в середине
    листа не уезжал к краю кадра по мере увеличения."""
    frames = max(1, int(round(hold_sec * 30)))
    zoom_max = 1.06  # мягкий наезд на 6% — заметно, но не «слот-машина»
    rate = (zoom_max - 1.0) / frames
    vf = (
        "scale=1080:1920,"
        f"zoompan=z='min(zoom+{rate:.8f},{zoom_max})':d=1:"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30,"
        "format=yuv420p"
    )
    # stinger="" — без звукового акцента: в хвосте тишина (поверх неё потом
    # ложится музыка), без резкого «дзынь».
    audio_in = (["-i", stinger] if stinger
                else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
    _ff(
        "-loop", "1", "-framerate", "30", "-t", f"{hold_sec}", "-i", still,
        *audio_in,
        "-vf", vf, "-af", "apad",
        "-t", f"{hold_sec}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-video_track_timescale", "90000",
        out_path,
    )


def _concat(parts: list, out_path: str) -> str:
    lst = out_path + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for n in parts:
            # в concat-листе кавычка внутри пути экранируется как '\''
            safe = os.path.abspath(n).replace("'", r"'\''")
            f.write(f"file '{safe}'\n")
    _ff("-f", "concat", "-safe", "0", "-i", lst, "-fflags", "+genpts", "-c", "copy", out_path)
    return lst


def assemble(segments: list, out_path: str, hold_sec: float = 2.0, music: str = "") -> str:
    """Склейка сегментов + стоп-кадр в конце (с наездом камеры и дзынь-акцентом),
    чтобы прогнозы успели прочитать."""
    ensure_tools()
    segments = [s for s in segments if s and os.path.exists(s) and os.path.getsize(s) > 0]
    if not segments:
        raise VideoError("Нечего склеивать: ни одного готового сегмента")

    work = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(work, exist_ok=True)
    temp: list[str] = []
    try:
        norm = []
        for i, s in enumerate(segments):
            n = os.path.join(work, f"_norm{i}.mp4")
            _normalize(s, n)
            norm.append(n)
        temp += norm

        if len(norm) == 1:
            joined = norm[0]
        else:
            joined = os.path.join(work, "_joined.mp4")
            lst = _concat(norm, joined)
            temp += [lst, joined]

        hold_sec = max(0.0, hold_sec)
        if hold_sec > 0:
            # Стоп-кадр строим ОТДЕЛЬНЫМ клипом (наезд + звук), а не tpad-
            # заморозкой внутри общего потока — так Ken Burns и дзынь не
            # затрагивают исходные сегменты, только хвост ролика.
            last_png = os.path.join(work, "_last.png")
            _ff("-sseof", "-0.1", "-i", joined, "-update", "1", "-frames:v", "1", last_png)
            # Звуковой акцент «дзынь» в начале стоп-кадра по умолчанию ВЫКЛЮЧЕН
            # (на ролике он слышен как резкий писк); HOLD_STINGER=true вернёт.
            stinger = ""
            if env_bool("HOLD_STINGER", False):
                stinger = os.path.join(work, "_stinger.wav")
                _stinger_wav(stinger)
            hold = os.path.join(work, "_hold.mp4")
            _hold_clip(last_png, stinger, hold_sec, hold)
            temp += [last_png, hold] + ([stinger] if stinger else [])

            full = os.path.join(work, "_full.mp4")
            lst2 = _concat([joined, hold], full)
            temp += [lst2, full]
        else:
            full = joined

        if music and os.path.exists(music):
            # MUSIC_OFFSET — с какой секунды трека начинать: так пик/дроп
            # трека можно подвести ровно под момент раскрытия вердикта
            # (начало hold-клипа), а не всегда слушать начало файла. atrim
            # идёт ПОСЛЕ stream_loop, поэтому смещение работает даже если
            # сам трек короче видео. Видео-дорожка из full просто копируется
            # (наезд и звук-дзынь уже «запечены» в hold-клип) — микшируется
            # только звук, поверх уже готовой дорожки с дзынем.
            offset = env_float("MUSIC_OFFSET", 0.0, lo=0.0, hi=600.0)
            _ff("-i", full, "-stream_loop", "-1", "-i", music,
                "-filter_complex",
                f"[1:a]atrim=start={offset},asetpts=PTS-STARTPTS,"
                f"volume={env_float('MUSIC_VOLUME', 0.25, lo=0.0, hi=1.0)}[m];"
                f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", out_path)
        else:
            _ff("-i", full, "-c", "copy", "-movflags", "+faststart", out_path)
    finally:
        for p in temp:
            if p == out_path:
                continue
            try:
                os.remove(p)
            except OSError:
                pass
    return out_path
