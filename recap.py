"""Итоги прогнозов CoinPlay AI → Threads / X.

Раз в сутки берёт из CoinPlay API проверенные прогнозы (grades) за вчера и
публикует текстовый пост «итоги вчера» по вертикалям; по понедельникам —
недельную сводку. В пост попадают только реальные цифры `coinplay_ai`
(правильно / всего, включая промахи); процент показывается, только если
выборка не меньше порога.

API ходит по голому IP со своим сертификатом, поэтому вместо отключения
проверки TLS сертификат закрепляется по отпечатку (COINPLAY_API_CERT_SHA256).
"""

import hashlib
import http.client
import json
import logging
import ssl
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import config
import db

log = logging.getLogger("recap")

MAX_PAGES = 20
PAGE_LIMIT = 200

# Подписи вертикалей: код из API -> (эмодзи, название по языкам)
_LABELS = {
    "soccer": ("⚽", {"es": "Fútbol", "en": "Football"}),
    "ufc": ("🥊", {"es": "UFC", "en": "UFC"}),
    "tennis": ("🎾", {"es": "Tenis", "en": "Tennis"}),
    "dota2": ("🎮", {"es": "Dota 2", "en": "Dota 2"}),
    "cs2": ("🔫", {"es": "CS2", "en": "CS2"}),
    "lol": ("🎮", {"es": "LoL", "en": "LoL"}),
    "valorant": ("🎯", {"es": "Valorant", "en": "Valorant"}),
}

_TEXTS = {
    "es": {
        "daily_title": "📊 Resultados de ayer · Coinplay AI",
        "weekly_title": "📈 Resumen semanal · Coinplay AI",
        "hits": "aciertos",
        "acc": "acumulado",
        "note_daily": "Cifras reales: aciertos y fallos.",
        "note_weekly": "Últimos 7 días. Cifras reales: aciertos y fallos.",
    },
    "en": {
        "daily_title": "📊 Yesterday's results · Coinplay AI",
        "weekly_title": "📈 Weekly summary · Coinplay AI",
        "hits": "hits",
        "acc": "overall",
        "note_daily": "Real numbers: hits and misses.",
        "note_weekly": "Last 7 days. Real numbers: hits and misses.",
    },
}


class RecapError(RuntimeError):
    pass


# --- HTTP-клиент с закреплением сертификата ---

def _norm_fp(fp: str) -> str:
    return (fp or "").replace(":", "").strip().lower()


def _connect() -> http.client.HTTPSConnection:
    parts = urlsplit(config.COINPLAY_API_URL)
    host, port = parts.hostname, parts.port or 443
    pin = _norm_fp(config.COINPLAY_API_CERT_SHA256)
    if pin:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # доверие даёт только совпадение отпечатка
    else:
        ctx = ssl.create_default_context()  # обычная проверка (для домена с нормальным сертификатом)
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=30)
    conn.connect()
    if pin:
        der = conn.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(der).hexdigest()
        if actual != pin:
            conn.close()
            raise RecapError(
                "Отпечаток сертификата API не совпадает с COINPLAY_API_CERT_SHA256 "
                f"(получен {actual}). Ключ не отправлен."
            )
    return conn


def _get(path: str, params: dict = None, _retries: int = 3) -> dict:
    parts = urlsplit(config.COINPLAY_API_URL)
    url_path = parts.path.rstrip("/") + "/" + path.lstrip("/")
    if params:
        url_path += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
    for attempt in range(_retries):
        conn = _connect()
        try:
            conn.request("GET", url_path, headers={
                "Authorization": f"Bearer {config.COINPLAY_API_KEY}",
                "Accept": "application/json",
            })
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
            retry_after = resp.getheader("Retry-After")
        finally:
            conn.close()
        if status == 429 and attempt < _retries - 1:
            time.sleep(min(int(retry_after or 5), 60))
            continue
        if status != 200:
            raise RecapError(f"API {path} -> HTTP {status}: {body[:200]}")
        return json.loads(body)
    raise RecapError(f"API {path}: слишком много повторов")


def _get_all(path: str, params: dict) -> list:
    """Все записи ресурса: ходим по next_cursor, snapshot передаём дальше."""
    rows, page_params = [], dict(params, limit=PAGE_LIMIT)
    for _ in range(MAX_PAGES):
        data = _get(path, page_params)
        rows.extend(data.get("data") or [])
        cursor = data.get("next_cursor")
        if not cursor:
            break
        page_params = dict(page_params, cursor=cursor)
        if data.get("snapshot"):
            page_params["snapshot"] = data["snapshot"]
    return rows


# --- Данные ---

def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fetch_grades(discipline: str, start: datetime, end: datetime) -> list:
    """Проверенные прогнозы вертикали за [start, end). since/until отдаём API,
    но на случай, если фильтр не сработал, окно проверяем ещё и здесь."""
    params = {"discipline": discipline,
              "since": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "until": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        rows = _get_all("grades", params)
    except RecapError as e:
        if "invalid_filter" not in str(e):
            raise
        log.warning("API не принял since/until — тяну без них и режу окно сам")
        rows = _get_all("grades", {"discipline": discipline})
    out, seen = [], set()
    for r in rows:
        d = r.get("data") or {}
        ts = _parse_ts(d.get("graded_at") or r.get("at"))
        mid = d.get("match_id") or r.get("id")
        if ts is None or not (start <= ts < end) or mid in seen:
            continue
        seen.add(mid)
        out.append(d)
    return out


def fetch_totals() -> dict:
    """Накопленная точность coinplay_ai: {discipline: (correct, total)}."""
    totals = {}
    for r in _get_all("quality", {}):
        d = r.get("data") or {}
        if d.get("model") != "coinplay_ai":
            continue
        disc = (r.get("discipline") or d.get("title") or "").lower()
        if d.get("total"):
            totals[disc] = (int(d.get("correct") or 0), int(d["total"]))
    return totals


def tally(grades: list) -> tuple:
    """(верно, всего) по coinplay_ai. Прогнозы без своего пика (None) не считаем."""
    judged = [g for g in grades if g.get("coinplay_ai_correct") in (True, False)]
    return sum(1 for g in judged if g["coinplay_ai_correct"]), len(judged)


# --- Тексты ---

def _label(disc: str, lang: str) -> str:
    emoji, names = _LABELS.get(disc, ("🏆", {}))
    return f"{emoji} {names.get(lang) or names.get('en') or disc.upper()}"


def _pct(correct: int, total: int) -> str:
    return f"{round(100 * correct / total)}%"


def build_daily(per_disc: dict, totals: dict, lang: str = None,
                with_total: bool = True) -> str:
    """per_disc: {discipline: (correct, total)} за вчера. Пустые вертикали пропускаем."""
    lang = lang or config.RECAP_LANG
    t = _TEXTS.get(lang) or _TEXTS["en"]
    lines = []
    for disc, (c, n) in per_disc.items():
        if n <= 0:
            continue
        line = f"{_label(disc, lang)}: {c}/{n} {t['hits']}"
        tc, tn = totals.get(disc, (0, 0))
        if with_total and tn >= config.RECAP_MIN_SAMPLE:
            line += f" · {t['acc']} {_pct(tc, tn)} ({tc}/{tn})"
        lines.append(line)
    if not lines:
        return ""
    parts = [t["daily_title"], "", *lines, "", t["note_daily"]]
    if config.RECAP_FOOTER:
        parts.append(config.RECAP_FOOTER)
    return "\n".join(parts)


def build_weekly(per_disc: dict, totals: dict, lang: str = None) -> str:
    lang = lang or config.RECAP_LANG
    t = _TEXTS.get(lang) or _TEXTS["en"]
    lines = []
    for disc, (c, n) in per_disc.items():
        if n <= 0:
            continue
        line = f"{_label(disc, lang)}: {c}/{n}"
        if n >= config.RECAP_WEEKLY_MIN_SAMPLE:
            line += f" ({_pct(c, n)})"
        tc, tn = totals.get(disc, (0, 0))
        if tn >= config.RECAP_MIN_SAMPLE:
            line += f" · {t['acc']} {_pct(tc, tn)}"
        lines.append(line)
    if not lines:
        return ""
    parts = [t["weekly_title"], "", *lines, "", t["note_weekly"]]
    if config.RECAP_FOOTER:
        parts.append(config.RECAP_FOOTER)
    return "\n".join(parts)


# --- Окна и расписание ---

def _tz() -> ZoneInfo:
    return ZoneInfo(config.RECAP_TZ)


def day_window(now: datetime, days: int = 1) -> tuple:
    """[00:00 `days` суток назад, 00:00 сегодня) по RECAP_TZ."""
    local = now.astimezone(_tz())
    end = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=days), end


def collect(days: int = 1, now: datetime = None) -> tuple:
    """({discipline: (верно, всего)}, {discipline: накопленно})."""
    now = now or datetime.now(timezone.utc)
    start, end = day_window(now, days)
    per_disc = {}
    for disc in config.RECAP_DISCIPLINES:
        per_disc[disc] = tally(fetch_grades(disc, start, end))
    totals = fetch_totals()
    return per_disc, totals


def compose(kind: str, now: datetime = None) -> dict:
    """{'text': полный текст, 'text_x': вариант под X} — пустые, если публиковать нечего."""
    days = 7 if kind == "weekly" else 1
    per_disc, totals = collect(days, now)
    if kind == "weekly":
        return {"text": build_weekly(per_disc, totals),
                "text_x": build_weekly(per_disc, totals)}
    return {"text": build_daily(per_disc, totals),
            # в X лимит 280: накопленную точность оставляем, только если вертикаль одна
            "text_x": build_daily(per_disc, totals,
                                  with_total=len(config.RECAP_DISCIPLINES) == 1)}


# --- Публикация ---

def _platforms() -> list:
    out = []
    if config.THREADS_ENABLED and "threads" in config.RECAP_PLATFORMS:
        out.append("threads")
    if config.X_ENABLED and "x" in config.RECAP_PLATFORMS:
        out.append("x")
    return out


def _publish(platform: str, text: str, text_x: str):
    if platform == "threads":
        import threads_api
        tag = threads_api.clean_topic_tag(config.RECAP_THREADS_TAG) or None
        return threads_api.publish(text, [], topic_tag=tag)
    if platform == "x":
        import x_api
        acc = x_api.X_DEFAULT_ACCOUNT or next(iter(x_api.X_ACCOUNTS.values()), None)
        body = text_x
        if config.RECAP_X_HASHTAGS:
            body = f"{body}\n{config.RECAP_X_HASHTAGS}"
        return x_api.publish(body, [], account=acc)
    raise RecapError(f"неизвестная площадка {platform}")


def _run(kind: str, key: str, now: datetime):
    """Публикует пост kind на площадки, где за `key` (дата/неделя) его ещё не было."""
    platforms = [p for p in _platforms()
                 if db.get_state(f"recap:{kind}:{p}") != key]
    if not platforms:
        return
    composed = compose(kind, now)
    if not composed["text"]:
        # публиковать нечего (нет проверенных матчей) — день закрываем без поста
        for p in platforms:
            db.set_state(f"recap:{kind}:{p}", key)
        log.info("Recap %s %s: проверенных прогнозов нет, пост пропущен", kind, key)
        return
    if config.RECAP_DRY_RUN:
        log.info("Recap %s %s (DRY RUN):\n%s", kind, key, composed["text"])
        for p in platforms:
            db.set_state(f"recap:{kind}:{p}", key)
        return
    failed = False
    for p in platforms:
        try:
            ids = _publish(p, composed["text"], composed["text_x"])
            db.set_state(f"recap:{kind}:{p}", key)
            log.info("Recap %s %s: опубликовано в %s %s", kind, key, p, ids)
        except Exception as e:
            failed = True
            log.error("Recap %s %s: %s — ошибка публикации: %s", kind, key, p, e)
    if failed:
        raise RecapError("часть площадок не опубликована")


def tick(now_ts: float = None):
    """Вызывается из воркера на каждом тике. Сам решает, пора ли."""
    if not config.RECAP_ENABLED:
        return
    now = datetime.fromtimestamp(now_ts or time.time(), timezone.utc)
    local = now.astimezone(_tz())
    due = []
    if local.hour >= config.RECAP_HOUR:
        due.append(("daily", local.date().isoformat()))
    if (config.RECAP_WEEKLY_DAY >= 0 and local.weekday() == config.RECAP_WEEKLY_DAY
            and local.hour >= min(config.RECAP_HOUR + 1, 23)):
        iso = local.isocalendar()
        due.append(("weekly", f"{iso[0]}-W{iso[1]:02d}"))

    pending = [(kind, key) for kind, key in due
               if any(db.get_state(f"recap:{kind}:{p}") != key for p in _platforms())]
    if not pending:
        return
    # после неудачи ждём RECAP_RETRY_COOLDOWN_SECONDS, чтобы не долбить API каждый тик
    last_fail = float(db.get_state("recap:last_failure", "0") or "0")
    if time.time() - last_fail < config.RECAP_RETRY_COOLDOWN_SECONDS:
        return
    for kind, key in pending:
        try:
            _run(kind, key, now)
        except Exception as e:
            db.set_state("recap:last_failure", str(time.time()))
            log.error("Recap %s: %s. Повтор не раньше чем через %d сек",
                      kind, e, config.RECAP_RETRY_COOLDOWN_SECONDS)
            return
