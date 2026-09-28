"""
Статистика опубликованных постов (просмотры/лайки) по площадкам.

Источник постов — таблица bursts в БД (db.posted_since): для каждой
опубликованной пачки там уже лежат id постов по площадкам
({'threads': [...], 'instagram': [...], 'x': [...]}). Сами просмотры/лайки
не хранятся — при каждом сборе отчёта они запрашиваются заново у каждой
площадки по этим id, так что цифры всегда свежие на момент отчёта.
"""

import json
import logging
import time

import requests

import db
import instagram_api
import threads_api
import x_api
from config import STATS_CHAT_ID, TELEGRAM_BOT_TOKEN

log = logging.getLogger("stats")

PLATFORM_LABELS = {"threads": "Threads", "instagram": "Instagram", "x": "X"}

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _empty_platform() -> dict:
    return {"posts": 0, "views": 0, "likes": 0, "errors": 0}


def collect(days: int = 7) -> dict:
    """Собирает и агрегирует статистику по всем площадкам за последние `days` дней."""
    cutoff = time.time() - days * 86400
    bursts = db.posted_since(cutoff)

    ids_by_platform = {"threads": [], "instagram": [], "x": []}
    for b in bursts:
        for platform, ids in (b.get("results") or {}).items():
            if platform in ids_by_platform and ids:
                ids_by_platform[platform].extend(ids)

    report = {p: _empty_platform() for p in ids_by_platform}

    # X отдаёт метрики батчами по 100 id одним запросом.
    x_ids = ids_by_platform["x"]
    if x_ids:
        report["x"]["posts"] = len(x_ids)
        try:
            metrics = x_api.get_metrics(x_ids)
            for m in metrics.values():
                report["x"]["views"] += m.get("views", 0)
                report["x"]["likes"] += m.get("likes", 0)
            missing = len(x_ids) - len(metrics)
            if missing:
                log.warning("X: метрики не найдены для %d из %d постов", missing, len(x_ids))
                report["x"]["errors"] += missing
        except Exception as e:
            log.error("X: не удалось получить метрики: %s", e)
            report["x"]["errors"] += len(x_ids)

    # У Threads и Instagram нет батч-эндпоинта для insights — только по одному id.
    for media_id in ids_by_platform["threads"]:
        report["threads"]["posts"] += 1
        try:
            m = threads_api.get_insights(media_id)
            report["threads"]["views"] += m.get("views", 0)
            report["threads"]["likes"] += m.get("likes", 0)
        except Exception as e:
            log.warning("Threads: метрики %s недоступны: %s", media_id, e)
            report["threads"]["errors"] += 1

    for media_id in ids_by_platform["instagram"]:
        report["instagram"]["posts"] += 1
        try:
            m = instagram_api.get_insights(media_id)
            report["instagram"]["views"] += m.get("views", 0)
            report["instagram"]["likes"] += m.get("likes", 0)
        except Exception as e:
            log.warning("Instagram: метрики %s недоступны: %s", media_id, e)
            report["instagram"]["errors"] += 1

    return {
        "days": days,
        "since": cutoff,
        "generated_at": time.time(),
        "platforms": report,
    }


def format_text(data: dict) -> str:
    lines = [f"📊 Статистика постов за последние {data['days']} дн.", ""]
    for key, label in PLATFORM_LABELS.items():
        p = data["platforms"].get(key, _empty_platform())
        lines.append(label)
        line = f"опубликовано {p['posts']} постов — {p['views']} просмотров — {p['likes']} лайков"
        if p["errors"]:
            line += f" (⚠️ {p['errors']} без данных)"
        lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


def send_to_telegram(data: dict, text: str = None) -> bool:
    """Шлёт текстовую сводку + тот же отчёт файлом .json в STATS_CHAT_ID."""
    if not (TELEGRAM_BOT_TOKEN and STATS_CHAT_ID):
        log.warning("TELEGRAM_BOT_TOKEN/STATS_CHAT_ID не заданы — отчёт не отправлен")
        return False

    text = text or format_text(data)

    r = requests.post(
        TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage"),
        data={"chat_id": STATS_CHAT_ID, "text": text},
        timeout=30,
    )
    if r.status_code >= 400:
        log.error("Telegram sendMessage -> %s: %s", r.status_code, r.text[:300])
        return False

    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    r = requests.post(
        TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="sendDocument"),
        data={"chat_id": STATS_CHAT_ID},
        files={"document": ("stats.json", payload, "application/json")},
        timeout=30,
    )
    if r.status_code >= 400:
        log.error("Telegram sendDocument -> %s: %s", r.status_code, r.text[:300])
        return False

    log.info("Статистика отправлена в чат %s", STATS_CHAT_ID)
    return True
