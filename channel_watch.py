"""Источник предиктов — публичный Telegram-канал CoinPlay AI (без токена).

У публичного канала есть веб-превью https://t.me/s/<канал> — последние ~20
постов обычным HTML, без авторизации и без Telegram-сессии. Забираем оттуда
посты "🤖 COINPLAY AI PREDICTIONS", разбираем их telegram_caption.py (тот же
парсер, что и для --caption-file) и отдаём generate.py в виде матчей.

Что в этом источнике есть и чего нет — см. docstring telegram_caption.py: в
тексте поста только агрегат панели (N из M, счёт, confidence, risk), без
построчного разбора по моделям — он есть только на картинке поста.

Каналы по умолчанию — те, что названы в README/памяти проекта; свои задаются
через WATCH_CHANNELS="football:coinplayfootballai,esports:coinplayesportai".
"""

from __future__ import annotations

import html
import logging
import re

import requests

import telegram_caption
from config import env_list, env_str

log = logging.getLogger("channel_watch")

DEFAULT_CHANNELS = {
    "football": "coinplayfootballai",
    "esports": "coinplayesportai",
    "ufc": "coinplayufcai",
}
HTTP_TIMEOUT = 20
_UA = {"User-Agent": "Mozilla/5.0 (compatible; ai-match-lab/1.0)"}

_MSG_SPLIT_RE = re.compile(r'(?=<div class="tgme_widget_message_wrap)')
_POST_ID_RE = re.compile(r'data-post="(?P<chan>[^"/]+)/(?P<id>\d+)"')
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(?P<body>.*?)</div>', re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_TIME_RE = re.compile(r'<time[^>]*datetime="(?P<dt>[^"]+)"')

# Заголовок поста с предиктом; Bet Builder, опросы и результаты — другие посты.
_PREDICTION_MARK = "COINPLAY AI PREDICTIONS"
# "FA Cup · Football" -> "FA Cup": вертикаль в подписи турнира лишняя.
_VERTICAL_SUFFIX_RE = re.compile(r"\s*·\s*(football|esports?|ufc|mma)\s*$", re.IGNORECASE)


class ChannelError(RuntimeError):
    pass


def channels_from_env(verticals: list[str]) -> dict[str, str]:
    """{"football": "coinplayfootballai", ...} для нужных вертикалей.

    WATCH_CHANNELS="football:имя,esports:имя" перекрывает дефолты."""
    chans = {v: DEFAULT_CHANNELS[v] for v in verticals if v in DEFAULT_CHANNELS}
    for pair in env_list("WATCH_CHANNELS"):
        v, _, name = pair.partition(":")
        v, name = v.strip().lower(), name.strip().lstrip("@")
        if v in verticals and name:
            chans[v] = name
    return chans


def fetch_html(channel: str) -> str:
    url = f"{env_str('TG_PREVIEW_BASE', 'https://t.me/s')}/{channel}"
    try:
        r = requests.get(url, headers=_UA, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise ChannelError(f"Не открыл {url}: {e}") from e
    if r.status_code != 200:
        raise ChannelError(f"{url} -> {r.status_code}")
    return r.text


def _text_of(body: str) -> str:
    body = _BR_RE.sub("\n", body)
    body = _TAG_RE.sub("", body)  # <i class="emoji"><b>🤖</b></i> -> 🤖
    return html.unescape(body).replace("\xa0", " ").strip()


def parse_posts(page: str) -> list[dict]:
    """HTML превью -> [{"channel","id","text","time"}], от старых к новым."""
    posts = []
    for chunk in _MSG_SPLIT_RE.split(page):
        pm = _POST_ID_RE.search(chunk)
        tm = _TEXT_RE.search(chunk)
        if not pm or not tm:
            continue  # служебные/медиа-посты без текста
        t = _TIME_RE.search(chunk)
        posts.append({"channel": pm.group("chan"), "id": int(pm.group("id")),
                      "text": _text_of(tm.group("body")),
                      "time": t.group("dt") if t else ""})
    posts.sort(key=lambda p: p["id"])
    return posts


def is_prediction(text: str) -> bool:
    return _PREDICTION_MARK in text.upper()


def match_from_post(post: dict, vertical: str) -> dict:
    """Пост -> match для generate.run(); бросает CaptionParseError."""
    key_id = f'{post["channel"]}-{post["id"]}'
    m = telegram_caption.match_from_caption(post["text"], vertical, match_id=key_id)
    m["competition"] = _VERTICAL_SUFFIX_RE.sub("", m["competition"]).strip()
    return m


def items(channels: dict[str, str], fetch=None) -> list[dict]:
    """Все предикты, сейчас видимые на превью каналов, от старых к новым:
    [{"vertical","key","match"}]; match=None, если пост не разобрался.

    Недоступный канал пропускаем (лог), остальные продолжают работать;
    если недоступны все — ChannelError."""
    fetch = fetch or fetch_html  # ищем в момент вызова — иначе подмену не подхватить
    out, errors = [], []
    for vertical, channel in channels.items():
        try:
            posts = parse_posts(fetch(channel))
        except ChannelError as e:
            errors.append(str(e))
            log.warning("%s", e)
            continue
        for p in posts:
            if not is_prediction(p["text"]):
                continue
            key = f'coinplay-caption-{p["channel"]}-{p["id"]}'
            try:
                match = match_from_post(p, vertical)
            except telegram_caption.CaptionParseError as e:
                log.warning("%s: пост не разобрал (%s)", key, e)
                match = None
            out.append({"vertical": vertical, "key": key, "match": match})
    if errors and not out and len(errors) == len(channels):
        raise ChannelError("; ".join(errors))
    return out
