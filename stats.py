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
import youtube_api
from config import STATS_CHAT_ID, TELEGRAM_BOT_TOKEN, YOUTUBE_ENABLED

log = logging.getLogger("stats")

PLATFORM_LABELS = {"threads": "Threads", "instagram": "Instagram", "x": "X",
                   "youtube": "YouTube Shorts"}

# Ссылки на наши аккаунты — используются в отчёте вместо "@username", чтобы
# Telegram не пытался резолвить его как упоминание тг-юзера (несуществующего
# и никак не связанного с реальным аккаунтом на площадке).
ACCOUNT_URLS = {
    "threads": "https://www.threads.com/@coinplayofficial",
    "instagram": "https://www.instagram.com/coinplayofficial/",
    "x": "https://x.com/thecoinplay",
    "youtube": "https://www.youtube.com/@hello-coinplay",
}

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _empty_platform() -> dict:
    return {"posts": 0, "views": 0, "likes": 0, "errors": 0}


def _account_usernames() -> dict:
    """Юзернеймы аккаунтов по площадкам — для подписи в отчёте. Ошибка на
    одной площадке не должна валить остальные, поэтому каждая — отдельный try."""
    out = {}
    try:
        out["threads"] = threads_api.whoami().get("username")
    except Exception as e:
        log.warning("Threads: не удалось узнать username: %s", e)
    try:
        out["instagram"] = instagram_api.whoami().get("username")
    except Exception as e:
        log.warning("Instagram: не удалось узнать username: %s", e)
    if YOUTUBE_ENABLED:
        out["youtube"] = "hello-coinplay"
    names = []
    for label, acc in x_api.all_accounts():
        try:
            names.append(str(x_api.whoami(acc).get("username")))
        except Exception as e:
            log.warning("X (%s): не удалось узнать username: %s", label, e)
    if names:
        out["x"] = ", @".join(names)   # format_text ставит "@" перед первым
    return out


def collect(days: int = 7) -> dict:
    """Собирает и агрегирует статистику по всем площадкам за последние `days` дней."""
    cutoff = time.time() - days * 86400
    bursts = db.posted_since(cutoff)
    accounts = _account_usernames()

    # id -> (burst_id, chat_id, title), чтобы после сбора метрик знать, какому
    # конкретно посту/источнику они принадлежат (не только сумму по площадке).
    index = {"threads": {}, "instagram": {}, "x": {}, "youtube": {}}
    for b in bursts:
        for platform, ids in (b.get("results") or {}).items():
            if platform not in index or not ids:
                continue
            for post_id in ids:
                index[platform][post_id] = {
                    "burst_id": b["id"],
                    "chat_id": b.get("chat_id"),
                    "title": b.get("title") or "",
                }

    report = {p: _empty_platform() for p in index}
    posts = []

    def _post_url(platform: str, post_id: str, api_url: str = None) -> str:
        if api_url:
            return api_url
        if platform == "x" and ACCOUNT_URLS.get("x"):
            return f"{ACCOUNT_URLS['x']}/status/{post_id}"
        if platform == "youtube":
            return f"https://www.youtube.com/shorts/{post_id}"
        return None

    def _record(platform: str, post_id: str, views: int, likes: int, ok: bool, url: str = None):
        meta = index[platform].get(post_id, {})
        posts.append({
            "platform": platform,
            "post_id": post_id,
            "chat_id": meta.get("chat_id"),
            "title": meta.get("title", ""),
            "views": views,
            "likes": likes,
            "ok": ok,
            "url": _post_url(platform, post_id, url),
        })

    # X отдаёт метрики батчами по 100 id одним запросом.
    x_ids = list(index["x"].keys())
    if x_ids:
        report["x"]["posts"] = len(x_ids)
        try:
            metrics = {}
            # Метрики читаются теми же ключами, которыми постили: группируем
            # твиты по аккаунту источника.
            by_account = {}
            for post_id in x_ids:
                acc = x_api.account_for(index["x"][post_id].get("chat_id"))
                by_account.setdefault(id(acc), (acc, []))[1].append(post_id)
            for acc, ids in by_account.values():
                try:
                    metrics.update(x_api.get_metrics(ids, acc))
                except Exception as e:
                    log.error("X: метрики не получены для %d постов: %s", len(ids), e)
                    report["x"]["last_error"] = str(e)
            for post_id in x_ids:
                m = metrics.get(post_id)
                if m is None:
                    report["x"]["errors"] += 1
                    _record("x", post_id, 0, 0, False)
                    continue
                report["x"]["views"] += m.get("views", 0)
                report["x"]["likes"] += m.get("likes", 0)
                _record("x", post_id, m.get("views", 0), m.get("likes", 0), True)
            missing = len(x_ids) - len(metrics)
            if missing:
                log.warning("X: метрики не найдены для %d из %d постов", missing, len(x_ids))
        except Exception as e:
            log.error("X: не удалось получить метрики: %s", e)
            report["x"]["errors"] += len(x_ids)
            for post_id in x_ids:
                _record("x", post_id, 0, 0, False)

    # У Threads и Instagram нет батч-эндпоинта для insights — только по одному id.
    for media_id in index["threads"]:
        report["threads"]["posts"] += 1
        try:
            m = threads_api.get_insights(media_id)
            report["threads"]["views"] += m.get("views", 0)
            report["threads"]["likes"] += m.get("likes", 0)
            _record("threads", media_id, m.get("views", 0), m.get("likes", 0), True, m.get("url"))
        except Exception as e:
            log.warning("Threads: метрики %s недоступны: %s", media_id, e)
            report["threads"]["errors"] += 1
            report["threads"]["last_error"] = str(e)
            _record("threads", media_id, 0, 0, False)

    for media_id in index["instagram"]:
        report["instagram"]["posts"] += 1
        try:
            m = instagram_api.get_insights(media_id)
            report["instagram"]["views"] += m.get("views", 0)
            report["instagram"]["likes"] += m.get("likes", 0)
            ok = not m.get("error")
            if not ok:
                # просмотры не пришли — это не «0 просмотров», а «нет данных»
                report["instagram"]["errors"] += 1
                report["instagram"]["last_error"] = m["error"]
            _record("instagram", media_id, m.get("views", 0), m.get("likes", 0), ok, m.get("url"))
        except Exception as e:
            log.warning("Instagram: метрики %s недоступны: %s", media_id, e)
            report["instagram"]["errors"] += 1
            report["instagram"]["last_error"] = str(e)
            _record("instagram", media_id, 0, 0, False)

    # YouTube: просмотры/лайки Shorts одним батч-запросом (до 50 id).
    yt_ids = list(index["youtube"].keys())
    if yt_ids:
        report["youtube"]["posts"] = len(yt_ids)
        try:
            metrics = youtube_api.get_metrics(yt_ids)
        except Exception as e:
            log.error("YouTube: не удалось получить метрики: %s", e)
            report["youtube"]["last_error"] = str(e)
            metrics = {}
        for video_id in yt_ids:
            m = metrics.get(video_id)
            if m is None:
                report["youtube"]["errors"] += 1
                _record("youtube", video_id, 0, 0, False)
                continue
            report["youtube"]["views"] += m["views"]
            report["youtube"]["likes"] += m["likes"]
            _record("youtube", video_id, m["views"], m["likes"], True)

    # По источнику (chat_id) — сколько постов оттуда ушло за период, по площадкам.
    by_source = {}
    for p in posts:
        chat_id = p["chat_id"]
        entry = by_source.setdefault(str(chat_id), {"title_examples": [], "platforms": {}})
        plat = entry["platforms"].setdefault(p["platform"], {"posts": 0, "views": 0, "likes": 0})
        plat["posts"] += 1
        plat["views"] += p["views"]
        plat["likes"] += p["likes"]
        if p["title"] and p["title"] not in entry["title_examples"] and len(entry["title_examples"]) < 3:
            entry["title_examples"].append(p["title"])

    return {
        "days": days,
        "since": cutoff,
        "generated_at": time.time(),
        "platforms": report,
        "accounts": accounts,
        "by_source": by_source,
        "posts": posts,
    }


def format_text(data: dict) -> str:
    """
    HTML (send_to_telegram шлёт с parse_mode=HTML) — юзернейм площадки
    кликабелен и ведёт на реальный аккаунт (ACCOUNT_URLS), а не на
    несуществующего тг-юзера, как было бы с голым "@username".
    """
    import html as _html

    lines = [f"📊 Статистика постов за последние {data['days']} дн.", ""]
    accounts = data.get("accounts") or {}
    for key, label in PLATFORM_LABELS.items():
        p = data["platforms"].get(key, _empty_platform())
        if key == "youtube" and not (YOUTUBE_ENABLED or p["posts"]):
            continue  # YouTube не включён — не засоряем отчёт пустой строкой
        username = accounts.get(key)
        url = ACCOUNT_URLS.get(key)
        if username and url:
            handle = f'<a href="{_html.escape(url)}">@{_html.escape(username)}</a>'
            lines.append(f"{label} ({handle})")
        elif username:
            lines.append(f"{label} (@{_html.escape(username)})")
        else:
            lines.append(label)
        line = f"опубликовано {p['posts']} постов — {p['views']} просмотров — {p['likes']} лайков"
        if p["errors"]:
            line += f" (⚠️ {p['errors']} без данных)"
        lines.append(line)
        if p.get("last_error"):
            lines.append("причина: " + _html.escape(str(p["last_error"]).replace("\n", " ")[:220]))
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
        data={"chat_id": STATS_CHAT_ID, "text": text, "parse_mode": "HTML"},
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
