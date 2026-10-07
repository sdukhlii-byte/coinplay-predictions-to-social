"""AI Match Lab: матч -> прогнозы 5 моделей -> видео с рукой -> zip-кит -> Telegram.

Примеры:
  python generate.py --home Spain --away Argentina --home-flag es --away-flag ar \
      --competition "World Cup Final" --date 2026-07-19

  # пачка матчей из файла
  python generate.py --match-file matches.json

  # без ручного списка: сам подбирает ближайшие реальные матчи (нужен
  # FOOTBALL_DATA_API_KEY, см. fixtures.py) — это и есть команда для крона
  python generate.py --auto

  # готовые прогнозы CoinPlay AI вместо живого опроса моделей (нужен
  # SETS_API_TOKEN, см. coinplay_sets.py) — по одной вертикали за прогон
  python generate.py --sets-vertical football
  python generate.py --sets-vertical esports
  python generate.py --sets-vertical ufc

  # сервис для грабера: принимает zip-киты бота по HTTP и делает видео
  python generate.py --serve

  # постоянный режим: сам следит за публичным каналом CoinPlay AI и делает
  # видео, как только там выходит новый предикт (токен не нужен)
  python generate.py --watch

  # то же самое, но из текста поста "COINPLAY AI PREDICTIONS" (копия своего
  # Telegram-скрейпера) — когда нет SETS_API_TOKEN; агрегат вместо 10 строк
  # по моделям, см. telegram_caption.py
  python generate.py --caption-file post.txt --caption-vertical football

  # без видео (проверить бланк и промпт), или со своими цифрами без API моделей
  python generate.py ... --no-video
  python generate.py ... --scores "1-2,2-1,2-2,1-0,2-1"

Результат: out/<slug>/ с blank.jpg, filled.jpg, prompt.txt, video.mp4,
predictions.json и <slug>.zip — кит в формате автопостера
(kit.json + threads/ instagram/ x/). Если заданы TELEGRAM_BOT_TOKEN и
TELEGRAM_CHAT_ID — zip уходит в группу, откуда его забирает автопостер.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import zipfile

import requests
from PIL import Image, ImageDraw, ImageFont

import backgrounds
import channel_watch
import coinplay_sets
import fixtures
import kit_source
import poster
import predictions
import state
import telegram_caption
import video
from config import env_bool, env_float, env_int, env_list, env_str

log = logging.getLogger("aml")
HERE = os.path.dirname(os.path.abspath(__file__))

TELEGRAM_VIDEO_LIMIT = 49 * 1024 * 1024  # лимит бота на sendVideo — 50 МБ
FLAG_TIMEOUT = 20

# Эмодзи в заголовке поста/хуке — по вертикали; футбол остаётся дефолтом для
# обратной совместимости со старыми вызовами (--home/--away без vertical).
_VERTICAL_EMOJI = {"football": "⚽", "esports": "🎮", "ufc": "🥊"}
# Файл-подложка под устройство — по вертикали; обычные football-прогоны (без
# vertical) по-прежнему берут TABLE_IMAGE/дефолтный стол, как раньше.
_VERTICAL_TABLE = {
    "esports": os.path.join(HERE, "assets", "tables", "esports.jpg"),
    "ufc": os.path.join(HERE, "assets", "tables", "ufc.jpg"),
    "football": os.path.join(HERE, "assets", "tables", "football.jpg"),
}


# ---------------------------------------------------------------- флаги ----

def _open_image(data: bytes, as_svg: bool = False) -> Image.Image:
    if as_svg:
        import cairosvg
        data = cairosvg.svg2png(bytestring=data, output_width=800)
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGBA")


_PLACEHOLDER_SKIP = {"FC", "CF", "SC", "AC", "CD", "SD", "UD", "AS", "CLUB"}


def _placeholder(team: str) -> Image.Image:
    """Заглушка на случай, если для команды не нашлось реальной эмблемы.

    Раньше это была почти пустая светлая карточка с мелкой подписью — на
    реальной генерации видео-модель принимала её за незаполненный бокс и
    «дорисовывала» в неё то цифру, то случайный значок, то другой рисунок
    в разных кадрах одного и того же ролика (флаг обязан быть статичной
    фотографией, см. промпт в video.py). Причина — слишком мало визуальной
    информации: почти однотонное поле ей не за что «зацепиться».
    Монограмма в кружке даёт столько же плотности, сколько настоящий герб,
    так что модели больше нечего домысливать.
    """
    # Белый фон, не тёмный: эмблема попадает на белую карточку флага
    # (_flag_card), а не на тёмный корпус устройства, которого больше нет.
    img = Image.new("RGB", (600, 400), poster.WHITE)
    d = ImageDraw.Draw(img)
    # Свой акцентный цвет на команду — детерминированный (не hash(), тот
    # рандомизирован между запусками процесса), чтобы два безымянных клуба
    # в одной карточке не сливались в одинаковые кружки.
    palette = [poster.GOLD, poster.LILAC, poster.YELLOW, (224, 110, 94)]
    idx = sum(ord(c) for c in (team or "")) % len(palette)
    color = palette[idx]

    cx, cy, r = 300, 200, 130
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=poster.PANEL, outline=color, width=8)
    d.ellipse([cx - r + 18, cy - r + 18, cx + r - 18, cy + r - 18], outline=color, width=3)

    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ]+", team or "") if w.upper() not in _PLACEHOLDER_SKIP]
    if len(words) >= 2:
        letters = "".join(w[0] for w in words[:3]).upper()
    elif words:
        letters = words[0][:3].upper()
    else:
        letters = (team or "?")[:2].upper()
    try:
        f = poster._font("RussoOne-Regular.ttf", 108 if len(letters) <= 2 else 84)
    except poster.AssetMissing:
        f = ImageFont.load_default()
    d.text((cx, cy), letters, font=f, fill=poster.WHITE, anchor="mm")
    return img.convert("RGBA")


def load_flag(spec: str, team: str) -> Image.Image:
    """
    spec: ISO-код страны ("es", "gb-eng"), путь к файлу или URL эмблемы.
    Порядок: локальный файл -> assets/flags/<код>.png -> flagcdn -> flag-icons SVG -> заглушка.

    Любая осечка — заглушка с названием команды, а не исключение: из-за
    недоступного CDN раньше падал весь матч целиком.
    """
    spec = (spec or "").strip()

    if spec and os.path.exists(spec):
        try:
            with open(spec, "rb") as f:
                return _open_image(f.read(), as_svg=spec.lower().endswith(".svg"))
        except Exception as e:
            log.warning("Файл флага %s не открылся (%s)", spec, e)

    if spec.lower().startswith(("http://", "https://")):
        try:
            r = requests.get(spec, timeout=FLAG_TIMEOUT)
            r.raise_for_status()
            is_svg = spec.lower().endswith(".svg") or "svg" in r.headers.get("content-type", "")
            return _open_image(r.content, as_svg=is_svg)
        except Exception as e:
            log.warning("Эмблема по ссылке %s не загрузилась (%s)", spec, e)

    code = re.sub(r"[^a-z0-9-]", "", spec.lower())
    if code:
        cached = os.path.join(HERE, "assets", "flags", f"{code}.png")
        if os.path.exists(cached):
            try:
                with open(cached, "rb") as f:
                    return _open_image(f.read())
            except Exception as e:
                log.warning("Локальный флаг %s битый (%s)", cached, e)
        for url, svg in ((f"https://flagcdn.com/w640/{code}.png", False),
                         (f"https://raw.githubusercontent.com/lipis/flag-icons/main/flags/4x3/{code}.svg", True)):
            try:
                r = requests.get(url, timeout=FLAG_TIMEOUT)
                if r.ok and r.content:
                    return _open_image(r.content, as_svg=svg)
            except Exception:
                continue
        log.warning("Флаг %r не найден — рисую заглушку с названием", spec)

    return _placeholder(team)


# -------------------------------------------------------------- тексты -----

def _verdict(rows, home, away):
    outcomes = collections.Counter(
        home if r["home"] > r["away"] else away if r["away"] > r["home"] else "Draw" for r in rows)
    pick, n = outcomes.most_common(1)[0]
    scores = collections.Counter(f'{r["home"]}–{r["away"]}' for r in rows)
    top_score, k = scores.most_common(1)[0]
    pick_txt = "Draw" if pick == "Draw" else f"{pick} to win"
    return pick_txt, n, top_score, k


def _hero_score(rows: list, has_real_consensus: bool, home: str, away: str) -> tuple[int | None, int | None]:
    """Счёт/победитель для ГЛАВНОГО бокса на листе (poster.Match.hero_home/
    hero_away) — консенсус/большинство, а НЕ произвольно rows[0].

    При реальной живой разбивке (несколько разных моделей опрошены по
    отдельности — predictions.py) модели почти никогда не сходятся в одну
    точную строку, так что rows[0] ("что ответил первый по очереди ChatGPT")
    — случайный выбор, который может противоречить собственной подписи
    "N OF M MODELS AGREE". Берём самый частый счёт среди rows (тот же
    расчёт, что уже используется в тексте поста — _verdict/top_score), кроме
    случаев, когда консенсус уже готов (coinplay_sets.py) или строка всего
    одна (telegram_caption.py) — тогда это и так единственно верный ответ,
    пересчитывать нечего.
    """
    if has_real_consensus or len(rows) <= 1:
        if not rows:
            return None, None
        return rows[0]["home"], rows[0]["away"]
    _, _, top_score, _ = _verdict(rows, home, away)
    hh, aa = top_score.split("–")
    return int(hh), int(aa)


def _pretty_date(iso: str) -> str:
    """`2026-10-02` → `2 OCT 2026`: на экране это читают люди, а не парсер."""
    try:
        d = datetime.date.fromisoformat((iso or "").strip())
    except ValueError:
        return (iso or "").strip()
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    return f"{d.day} {months[d.month - 1]} {d.year}"


def consensus_text(rows: list, home: str, away: str) -> str:
    """Кого выбрали модели — для плашки на экране: «SÃO PAULO FC».

    Берём тот же расчёт, что и подпись поста (_verdict), чтобы экран и текст
    поста не могли разойтись между собой.
    """
    if not rows:
        return ""
    pick, _, _, _ = _verdict(rows, home, away)
    return "DRAW" if pick == "Draw" else pick.replace(" to win", "")


def consensus_note(rows: list, home: str, away: str) -> str:
    """Сила согласия моделей: «4 OF 5 MODELS AGREE».

    Своих коэффициентов у нас нет, а выдумывать их в iGaming-креативе нельзя,
    поэтому аргументом для зрителя служит единодушие моделей — величина,
    которую мы действительно посчитали.
    """
    if not rows:
        return ""
    _, n, _, _ = _verdict(rows, home, away)
    return f"{n} of {len(rows)} models agree"


def consensus_pct(rows: list, home: str, away: str) -> int:
    """Согласие моделей в процентах — то же число, что и в consensus_note,
    только для крупной цифры-«хиро» рядом с плашкой (round(n / total * 100)).

    Реальная величина, честно посчитанная из голосов моделей — не выдуманный
    коэффициент и не вероятность результата матча.
    """
    if not rows:
        return 0
    _, n, _, _ = _verdict(rows, home, away)
    return round(100 * n / len(rows))


def _row_pick(r: dict, home: str, away: str) -> str:
    return home if r["home"] > r["away"] else away


def captions(match: dict, rows: list) -> dict:
    home, away = match["home"], match["away"]
    vertical = match.get("vertical", "football")
    pick, n, top, k = _verdict(rows, home, away)
    total = len(rows)  # раньше было жёстко "/5" — при MIN_MODELS<5 подпись врала
    emoji = _VERTICAL_EMOJI.get(vertical, "⚽")

    if vertical == "ufc":
        # У UFC нет настоящего счёта (score в sets.json всегда null) — rows
        # хранят победителя как 1/0 (см. coinplay_sets.rows_from_predictions),
        # поэтому в подписи показываем имя победителя, а не "1–0".
        lines = "\n".join(f'{r["label"]} — {_row_pick(r, home, away)}' for r in rows)
        short_scores = " · ".join(f'{r["label"]} {_row_pick(r, home, away)}' for r in rows)
        top_line = ""
        cta_word = "pick"
    else:
        lines = "\n".join(f'{r["label"]} — {r["home"]}–{r["away"]}' for r in rows)
        short_scores = " · ".join(f'{r["label"]} {r["home"]}–{r["away"]}' for r in rows)
        top_line = f"\n🎯 Most common score: {top} ({k}/{total})" if k > 1 else ""
        cta_word = "score"

    head = f"🤖{emoji} {total} AI MODELS PREDICT: {home} vs {away}"
    sub = " · ".join(x for x in (match.get("competition"), match.get("date")) if x)

    long = (f"{head}\n{sub}\n\n{lines}\n\n"
            f"📊 AI consensus: {pick} ({n}/{total} models){top_line}\n\n"
            f"Which AI gets it right? Drop your {cta_word} 👇\n\n"
            f"🎁 Link in bio\n18+ | Analysis and entertainment only.")
    x = (f"🤖 {total} AIs predict {home} vs {away}\n\n{short_scores}\n\n"
         f"Consensus: {pick} ({n}/{total})\nYour {cta_word}? 👇\n\nLink in bio")
    if len(x) > 280:  # X режет длинные посты — подстраховываемся коротким вариантом
        x = f"🤖 {home} vs {away}\n{short_scores}\nConsensus: {pick} ({n}/{total})\nLink in bio"[:280]
    # YouTube: первая строка — название ролика (<=100), остальное — описание.
    # "Link in bio" в описании YouTube не работает — убираем.
    body = long.split("\n", 1)[1].lstrip("\n").replace("🎁 Link in bio\n", "")
    yt_title = f"🤖 {total} AI models predict: {home} vs {away}"
    youtube = f"{yt_title}\n\n{body}\n\n#Shorts #AI #predictions"
    return {"threads": long, "instagram": long, "x": x, "youtube": youtube}


# ----------------------------------------------------------------- кит -----

def _letters(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


def slugify(s: str) -> str:
    """ASCII-слаг.

    Для кириллицы/арабицы транслитерации нет: раньше слаг выходил пустым,
    out_dir совпадал с корневой папкой out/, а архив назывался ".zip" —
    следующий матч затирал предыдущий. Теперь потерянные при транслитерации
    имена компенсируются коротким хешем, а совсем пустой слаг заменяется целиком.
    """
    ascii_s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_s.lower()).strip("-")
    digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    if not slug:
        return f"match-{digest}"
    if _letters(ascii_s) < _letters(s):
        slug = f"{slug[:88]}-{digest}".strip("-")
    return slug[:100]


def _write_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_kit(out_dir: str, slug: str, match: dict, rows: list,
              video_path: str, cover: str) -> str:
    caps = captions(match, rows)
    kit = {
        "match_id": slug,
        "team_a": match["home"],
        "team_b": match["away"],
        "format": "ai-match-lab-video",
        "platforms": {},
    }
    zpath = os.path.join(out_dir, f"{slug}.zip")
    tmp = f"{zpath}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        has_video = bool(video_path and os.path.exists(video_path))
        # Рилсы — только туда, где они нужны (по умолчанию Instagram); в Threads
        # и X видео не кладём. Картиночный кит (без видео) идёт на все площадки.
        plats = (env_list("VIDEO_PLATFORMS", ["instagram"]) if has_video
                 else ["threads", "instagram", "x"])
        plats = [p.lower() for p in plats if p.lower() in caps]
        for plat in plats:
            z.writestr(f"{plat}/post.txt", caps[plat])
            spec = {"text_file": "post.txt"}
            if has_video:
                z.write(video_path, f"{plat}/video.mp4")
                spec["videos"] = ["video.mp4"]
            else:
                z.write(cover, f"{plat}/01.jpg")
                spec["images"] = ["01.jpg"]
            kit["platforms"][plat] = spec
        z.writestr("kit.json", json.dumps(kit, ensure_ascii=False, indent=2))
    os.replace(tmp, zpath)  # автопостер не подхватит недописанный архив
    return zpath


def _tg_post(api: str, method: str, **kw):
    """POST в Bot API. Ошибка — короткое сообщение с причиной от Telegram
    ("chat not found", "not enough rights"...), а НЕ исключение requests: в его
    тексте полный URL с токеном бота, и он попадал в логи."""
    try:
        r = requests.post(f"{api}/{method}", **kw)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", "")
        desc = ""
        if resp is not None:
            try:
                desc = (resp.json() or {}).get("description", "")
            except ValueError:
                desc = (getattr(resp, "text", "") or "")[:200]
        raise RuntimeError(
            f"Telegram {method}: {status} {desc}".strip() if (status or desc)
            else f"Telegram {method}: {type(e).__name__}") from None


def send_telegram(zpath: str, match: dict, preview: str = "") -> bool:
    # Свой чат под каждую вертикаль (TELEGRAM_CHAT_ID_FOOTBALL / _ESPORTS / _UFC):
    # автопостер читает отдельные группы-«approvals» по вертикалям, и кит по
    # UFC не должен попасть в футбольную. Нет своего — общий TELEGRAM_CHAT_ID.
    vertical = (match.get("vertical") or "").upper()
    token = env_str("TELEGRAM_BOT_TOKEN")
    chat = (env_str(f"TELEGRAM_CHAT_ID_{vertical}") if vertical else "") or env_str("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.info("TELEGRAM_BOT_TOKEN/CHAT_ID не заданы — в Telegram не отправляю")
        return False
    api = f"https://api.telegram.org/bot{token}"
    title = f'Coinplay AI Lab · {match["home"]} vs {match["away"]}'

    # Превью — приятный бонус, но если оно не ушло (тайм-аут, слишком большой
    # файл), кит всё равно должен попасть в группу: раньше исключение здесь
    # обрывало отправку архива.
    # По умолчанию выключено: превью без манифеста грабер публикует отдельным
    # рилсом, и в Instagram получается дубль кита.
    if preview and env_bool("TELEGRAM_SEND_PREVIEW", False) and os.path.exists(preview):
        size = os.path.getsize(preview)
        if size < TELEGRAM_VIDEO_LIMIT:
            try:
                with open(preview, "rb") as f:
                    _tg_post(api, "sendVideo",
                             data={"chat_id": chat, "caption": title,
                                   "supports_streaming": "true"},
                             files={"video": f}, timeout=300)
            except Exception as e:
                log.warning("Превью в Telegram не ушло (%s) — отправляю только кит", e)
        else:
            log.info("Превью %.1f МБ больше лимита бота — отправляю только кит",
                     size / 1024 / 1024)

    with open(zpath, "rb") as f:
        _tg_post(api, "sendDocument", data={"chat_id": chat, "caption": title},
                 files={"document": (os.path.basename(zpath), f, "application/zip")},
                 timeout=300)
    log.info("Кит отправлен в Telegram: %s", os.path.basename(zpath))
    return True


# ------------------------------------------------------------ пайплайн -----

def parse_scores(s: str, expected: int) -> list[tuple[int, int]]:
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        bits = re.split(r"\s*[-:–]\s*", part)
        if len(bits) != 2 or not all(b.strip().isdigit() for b in bits):
            raise ValueError(f'--scores: не понял счёт {part!r}, нужен вид "2-1"')
        out.append((int(bits[0]), int(bits[1])))
    if len(out) != expected:
        raise ValueError(f"--scores: нужно {expected} счетов через запятую, получено {len(out)}")
    return out


def _parse_scores(s: str) -> list[tuple[int, int]]:
    """Совместимость со старым именем."""
    return parse_scores(s, len(predictions.SLOTS))


def _check_date(match: dict, allow_past: bool) -> None:
    """
    Матч с прошедшей датой уже имеет реальный результат — модели этого не
    знают и просто нафантазируют правдоподобный "прогноз". Останавливаем до
    похода к API моделей, а не постфактум.
    """
    raw = (match.get("date") or "").strip()
    if not raw:
        return
    try:
        d = datetime.date.fromisoformat(raw)
    except ValueError:
        log.warning("%s vs %s: дату %r не разобрал (нужен формат YYYY-MM-DD) — не проверяю",
                    match["home"], match["away"], raw)
        return
    today = datetime.date.today()
    if d < today:
        msg = (f'{match["home"]} vs {match["away"]}: дата {raw} уже в прошлом '
               f'(сегодня {today.isoformat()}) — у матча есть реальный результат, '
               f'прогноз бессмыслен')
        if allow_past:
            log.warning("%s — пропускаю проверку (ALLOW_PAST_DATES=true)", msg)
        else:
            raise ValueError(msg)


def _rows_for(match: dict) -> list[dict]:
    if match.get("rows"):
        # Готовые прогнозы — уже посчитаны (coinplay_sets.py из sets.json или
        # любой другой вызывающий код, который сам подготовил rows): не
        # дёргаем ни ручной --scores парсинг, ни живой опрос моделей.
        return match["rows"]
    if match.get("scores"):
        pairs = parse_scores(match["scores"], len(predictions.SLOTS))
        return [{"label": s[0], "icon": s[1], "model": "manual", "home": h, "away": a, "reason": ""}
                for s, (h, a) in zip(predictions.SLOTS, pairs)]
    return predictions.predict_all(match)


def run(match: dict, args) -> str:
    _check_date(match, allow_past=env_bool("ALLOW_PAST_DATES", False))
    slug = slugify(f'{match["home"]}-vs-{match["away"]}-{match.get("date", "")}')
    out_dir = os.path.join(args.out, slug)
    os.makedirs(out_dir, exist_ok=True)
    log.info("=== %s vs %s -> %s", match["home"], match["away"], out_dir)

    if not args.no_video:
        video.ensure_tools()  # проверяем ffmpeg ДО того, как потратим деньги на модели

    # 1. прогнозы
    rows = _rows_for(match)
    _write_json(os.path.join(out_dir, "predictions.json"), {"match": match, "rows": rows})

    # 2. кадры
    # Если консенсус уже посчитан поставщиком данных (coinplay_sets.py — тот
    # же вывод, что люди уже видели в посте CoinPlay AI), используем его
    # как есть, а не пересчитываем заново из rows: для UFC rows хранят
    # победителя как 1/0-заглушку счёта (см. coinplay_sets.rows_from_predictions),
    # и честный consensus.votes из sets.json точнее, чем производная от неё.
    has_real_consensus = "consensus" in match
    # match["hero"] — готовый главный счёт от источника (kit_source: самый частый
    # среди проголосовавших за победителя), приоритетнее любого пересчёта.
    hero_h, hero_a = match.get("hero") or _hero_score(
        rows, has_real_consensus, match["home"], match["away"])
    m = poster.Match(
        home=match["home"], away=match["away"],
        home_flag=load_flag(match.get("home_flag", ""), match["home"]),
        away_flag=load_flag(match.get("away_flag", ""), match["away"]),
        rows=[poster.Row(r["label"], r["home"], r["away"], r["icon"]) for r in rows],
        # Готовый subtitle (например telegram_caption.py — "9 OF 10 MODELS
        # AGREE", одна агрегированная строка вместо 10 построчных) побеждает;
        # иначе прежняя логика "N AI MODELS PREDICT" по числу строк.
        subtitle=match.get("subtitle") or f"{len(rows)} AI MODELS PREDICT",
        competition=match.get("competition", ""),
        date=_pretty_date(match.get("date", "")),
        consensus=match["consensus"] if has_real_consensus
                 else consensus_text(rows, match["home"], match["away"]),
        consensus_note=match.get("consensus_note", "") if has_real_consensus
                       else consensus_note(rows, match["home"], match["away"]),
        consensus_pct=match.get("consensus_pct", 0) if has_real_consensus
                     else consensus_pct(rows, match["home"], match["away"]),
        vertical=match.get("vertical", ""),
        hero_home=hero_h, hero_away=hero_a,
    )
    # Подложка — по вертикали; TABLE_IMAGE_<VERTICAL> (например
    # TABLE_IMAGE_ESPORTS) даёт заменить её на свою/фото без изменений в
    # коде, запасной вариант — собранная процедурно assets/tables/<v>.jpg.
    # Без vertical (старые ручные прогоны) — прежнее поведение: TABLE_IMAGE
    # с дефолтным assets/table.jpg.
    vertical = match.get("vertical", "")
    if vertical:
        table = env_str(f"TABLE_IMAGE_{vertical.upper()}", _VERTICAL_TABLE.get(vertical, ""))
    else:
        table = env_str("TABLE_IMAGE", os.path.join(HERE, "assets", "table.jpg"))
    # AI-фон ПОД ЭТОТ МАТЧ (флаги/цвета команд в толпе — как на референсе:
    # "Бельгия -> бельгийские флаги на трибунах"), а не один статичный файл
    # на всю вертикаль. Платный вызов Gemini, поэтому по умолчанию выключен
    # (AI_BACKGROUND=0) — включается явно, и даже тогда --no-ai-background
    # на конкретном прогоне берёт верх (для бесплатных тестов). Результат
    # кешируется в out_dir/_bg.jpg — повторный прогон того же матча не
    # платит за фон ещё раз, если не передан --fresh-background.
    if (vertical and env_bool("AI_BACKGROUND", False)
            and not getattr(args, "no_ai_background", False)):
        bg_path = os.path.join(out_dir, "_bg.jpg")
        if getattr(args, "fresh_background", False) or not os.path.exists(bg_path):
            backgrounds.generate_to_file(vertical, match["home"], match["away"], bg_path)
        if os.path.exists(bg_path):
            table = bg_path
    # Лист показывает ОДИН бокс со счётом/победителем (см. poster.render_paper),
    # а не построчную таблицу по моделям — значит и ролик всегда один сегмент
    # "пусто -> заполнено", независимо от того, сколько строк посчитал
    # источник данных (rows бывает длиннее 1 — тогда они идут в полоску
    # мини-иконок под боксом, см. poster._model_strip). Раньше это решала
    # отдельная "нарезка на сегменты" (_segments/SEGMENTS) — вестигиальная с
    # тех пор, как на листе осталась одна клетка; убрана вместе с мёртвым
    # SEGMENTS/PREFILL_ROWS.
    blank = poster.compose_frame(poster.render_paper(m, filled_rows=0), table)
    filled = poster.compose_frame(poster.render_paper(m, filled_rows=1), table)
    blank_path = os.path.join(out_dir, "blank.jpg")
    filled_path = os.path.join(out_dir, "filled.jpg")
    blank.save(blank_path, quality=93)
    filled.save(filled_path, quality=93)

    prompt = video.build_prompt(m.vertical, hero_h, hero_a, match["home"], match["away"])
    with open(os.path.join(out_dir, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)

    # 3. видео
    video_path = ""
    parts: list[str] = []
    # Повтор того же кита (например, упала отправка в Telegram) не должен снова
    # платить за ролик: если в out_dir уже лежит video.mp4 от ТОЧНО таких же
    # входных данных (подпись ниже), берём его. Другие счета -> другая подпись.
    sig = hashlib.sha1(json.dumps(
        [prompt, hero_h, hero_a, [(r["label"], r["home"], r["away"]) for r in rows],
         match["home"], match["away"]], ensure_ascii=False).encode()).hexdigest()
    vid_final = os.path.join(out_dir, "video.mp4")
    sig_path = os.path.join(out_dir, "video.sig")
    cached = False
    if not args.no_video and os.path.exists(vid_final) and os.path.getsize(vid_final) > 0:
        try:
            with open(sig_path, encoding="utf-8") as f:
                cached = f.read().strip() == sig
        except OSError:
            cached = False
    if cached:
        log.info("Видео уже есть для тех же данных — не генерирую заново: %s", vid_final)
        video_path = vid_final
    elif not args.no_video:
        try:
            p = os.path.join(out_dir, "_seg0.mp4")
            video.generate_segment(blank, filled, prompt, p)
            parts.append(p)
            video_path = video.assemble(parts, os.path.join(out_dir, "video.mp4"),
                                        hold_sec=env_float("HOLD_SEC", 2.0, lo=0.0, hi=15.0),
                                        music=video.pick_music(seed=str(match.get("id", ""))))
            try:
                with open(sig_path, "w", encoding="utf-8") as f:
                    f.write(sig)
            except OSError:
                pass
        finally:
            if not args.keep_temp:
                for p in parts:  # промежуточные сегменты раньше оставались в out/ навсегда
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # 4. кит + Telegram
    zpath = build_kit(out_dir, slug, match, rows, video_path, filled_path)
    log.info("Кит: %s", zpath)
    if not args.no_send:
        send_telegram(zpath, match, video_path)
    return zpath


def _load_match_file(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise SystemExit(f"{path}: ожидался JSON-список объектов матчей")
    bad = [i for i, m in enumerate(data) if not (m.get("home") and m.get("away"))]
    if bad:
        raise SystemExit(f"{path}: в записях {bad} нет обязательных полей home/away")
    return data


BASELINE_PREFIX = "watch-baseline-"


def _sets_items(data: dict, verticals: list[str]) -> list[dict]:
    """sets.json -> общий вид предиктов [{"vertical","key","match"}]."""
    return [{"vertical": s["vertical"], "key": coinplay_sets.set_key(s),
             "match": coinplay_sets.match_from_set(s)}
            for s in coinplay_sets.watch_sets(data, verticals)]


def _watch_baseline(items: list[dict], verticals: list[str]) -> None:
    """При первом старте по вертикали помечаем ВСЕ уже опубликованные предикты
    как виденные, не рисуя их: источник отдаёт и старые посты, и без этого
    первый же запуск заказал бы видео (платные!) по всем им сразу."""
    for v in verticals:
        marker = BASELINE_PREFIX + v
        if state.has_marker(marker):
            continue
        keys = {it["key"] for it in items if it["vertical"] == v}
        state.mark_many(keys)
        state.set_marker(marker)
        log.info("watch: baseline %s — %d уже опубликованных предиктов помечено как "
                 "виденные (видео по ним не делаем, ждём только новые)", v, len(keys))


def watch_cycle(args, items: list[dict], attempts: collections.Counter,
                per_cycle: int = 3, max_attempts: int = 2,
                bad_logged: set | None = None) -> tuple[list[str], list[str]]:
    """Один проход: новые предикты -> видео -> Telegram.

    Упавший матч повторяем не больше max_attempts раз (каждая попытка — это
    платный вызов видео-модели), потом помечаем как обработанный и идём дальше.
    """
    gave_up = {k for k, n in attempts.items() if n >= max_attempts}
    seen = state.already_posted({it["key"] for it in items}) | gave_up
    fresh = [it for it in items if it["key"] not in seen]
    done, failed = [], []
    todo = []
    for it in fresh:
        if it["match"] is None:
            if bad_logged is not None and it["key"] not in bad_logged:
                bad_logged.add(it["key"])
                log.warning("watch: %s — не удалось разобрать, пропускаю", it["key"])
            continue
        todo.append(it)
    for it in todo[:per_cycle]:
        mt, key = it["match"], it["key"]
        label = f'{mt["home"]} vs {mt["away"]}'
        try:
            run(mt, args)
            state.mark_posted(key, competition=mt.get("competition", ""))
            done.append(label)
        except Exception as e:
            attempts[key] += 1
            failed.append(label)
            log.exception("watch: матч %s не собран (попытка %d/%d): %s",
                          label, attempts[key], max_attempts, e)
            if attempts[key] >= max_attempts:
                state.mark_posted(key, competition=mt.get("competition", ""))
                log.error("watch: %s — больше не пытаюсь (исчерпано %d попыток)", label, max_attempts)
    return done, failed


def _watch_source(source: str, verticals: list[str]):
    """-> (описание, функция «получить все текущие предикты»)."""
    if source == "sets":
        coinplay_sets._headers()  # нет SETS_API_TOKEN — падаем сразу и внятно
        return "sets.json", lambda: _sets_items(coinplay_sets.fetch_raw(), verticals)
    chans = channel_watch.channels_from_env(verticals)
    if not chans:
        raise RuntimeError(f"Для вертикалей {verticals} нет канала (см. WATCH_CHANNELS)")
    desc = "каналы " + ", ".join(f"@{c}" for c in chans.values())
    return desc, lambda: channel_watch.items(chans)


def watch(args, sleep=time.sleep, max_cycles: int | None = None) -> None:
    """Постоянный режим: ждём новый предикт CoinPlay AI -> сразу видео.

    Источник — WATCH_SOURCE: `channel` (по умолчанию: публичное превью
    Telegram-канала, токен не нужен) или `sets` (sets.json, нужен
    SETS_API_TOKEN). Это сервис, а не крон: Cron Schedule у него должен быть
    пустым, иначе Railway будет останавливать процесс по расписанию.
    """
    verticals = [v for v in env_list("WATCH_VERTICALS", list(coinplay_sets.VERTICALS)) if v in coinplay_sets.VERTICALS]
    if not verticals:
        log.error("WATCH_VERTICALS должен содержать одну из %s", ", ".join(coinplay_sets.VERTICALS))
        sys.exit(1)
    source = env_str("WATCH_SOURCE", "channel").lower()
    if source not in ("channel", "sets"):
        log.error("WATCH_SOURCE должен быть channel или sets, а не %r", source)
        sys.exit(1)
    poll = env_int("WATCH_POLL_SEC", 60, lo=5, hi=3600)
    per_cycle = env_int("WATCH_PER_CYCLE", 3, lo=1, hi=20)
    max_attempts = env_int("WATCH_MAX_ATTEMPTS", 2, lo=1, hi=10)
    try:
        desc, fetch_items = _watch_source(source, verticals)
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)

    log.info("watch: слежу за %s (%s), опрос каждые %d c, не более %d видео за проход",
             desc, ", ".join(verticals), poll, per_cycle)
    base_chat = env_str("TELEGRAM_CHAT_ID")
    for v in verticals:
        own = env_str(f"TELEGRAM_CHAT_ID_{v.upper()}")
        log.info("watch: кит по %s уйдёт в чат %s%s", v, own or base_chat or "— (не задан)",
                 "" if own else " (общий TELEGRAM_CHAT_ID — если это не группа вертикали "
                                f"{v}, задай TELEGRAM_CHAT_ID_{v.upper()})")
    attempts: collections.Counter = collections.Counter()
    bad_logged: set = set()
    first, idle, cycles = True, 0, 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            items = fetch_items()
        except Exception as e:
            log.warning("watch: источник недоступен (%s) — повторю через %d c", e, poll)
            sleep(poll)
            continue
        if first:
            by_v = collections.Counter(it["vertical"] for it in items)
            ok = sum(1 for it in items if it["match"] is not None)
            log.info("watch: источник прочитан — предиктов сейчас видно %d, разобрано %d (%s)",
                     len(items), ok, ", ".join(f"{k}: {n}" for k, n in sorted(by_v.items())) or "пусто")
            if not env_bool("WATCH_BASELINE", True):
                log.warning("watch: WATCH_BASELINE=false — отрисую ВСЕ видимые предикты, включая старые")
                for v in verticals:
                    state.set_marker(BASELINE_PREFIX + v)
            else:
                _watch_baseline(items, verticals)
            first = False
        try:
            done, failed = watch_cycle(args, items, attempts, per_cycle, max_attempts, bad_logged)
        except Exception as e:  # цикл не должен умирать из-за одного кривого набора
            log.exception("watch: сбой прохода: %s", e)
            done, failed = [], []
        if done or failed:
            idle = 0
            log.info("watch: собрано %d, с ошибкой %d%s", len(done), len(failed),
                     (" — " + ", ".join(failed)) if failed else "")
        else:
            idle += 1
            if idle % max(1, 1800 // poll) == 1:  # раз в ~30 мин, чтобы лог не пух
                log.info("watch: новых предиктов нет, жду")
        sleep(poll)


def main() -> None:
    logging.basicConfig(level=env_str("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home")
    ap.add_argument("--away")
    ap.add_argument("--home-flag", default="", help="ISO-код (es), путь или URL эмблемы")
    ap.add_argument("--away-flag", default="")
    ap.add_argument("--competition", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--scores", default="", help='свои счета без API: "1-2,2-1,2-2,1-0,2-1"')
    ap.add_argument("--match-file", default="", help="JSON-список матчей с теми же полями")
    ap.add_argument("--auto", action="store_true",
                    help="не читать --match-file — самому подобрать ближайшие реальные "
                         "матчи через football-data.org (см. fixtures.py)")
    ap.add_argument("--watch", action="store_true",
                    help="постоянный режим: следить за публичным Telegram-каналом CoinPlay AI "
                         "(или sets.json при WATCH_SOURCE=sets) и делать видео по каждому "
                         "новому предикту (WATCH_VERTICALS, WATCH_POLL_SEC)")
    ap.add_argument("--serve", action="store_true",
                    help="постоянный сервис: принимать zip-киты бота по HTTP (POST /kit от "
                         "грабера) и делать по ним видео; нужен KIT_API_TOKEN (kit_server.py)")
    ap.add_argument("--kit", nargs="+", default=[], metavar="ZIP",
                    help="zip-кит(ы) бота CoinPlay AI («social kit: A vs B», внутри "
                         "blank/blank.json со счетами 10 моделей) — собрать видео по ним "
                         "(см. kit_source.py)")
    ap.add_argument("--sets-vertical", choices=coinplay_sets.VERTICALS, default="",
                    help="взять готовые прогнозы CoinPlay AI (sets.json) по этой "
                         "вертикали вместо football-data.org/живого опроса моделей — "
                         "нужен SETS_API_TOKEN (см. coinplay_sets.py)")
    ap.add_argument("--caption-file", default="",
                    help="текстовый файл с постом 'COINPLAY AI PREDICTIONS' из "
                         "Telegram-канала коллеги (копия/вывод своего скрейпера) — "
                         "альтернатива --sets-vertical, когда нет SETS_API_TOKEN; "
                         "нужен --caption-vertical (см. telegram_caption.py)")
    ap.add_argument("--caption-vertical", choices=telegram_caption.VERTICALS, default="",
                    help="вертикаль поста для --caption-file")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--no-video", action="store_true", help="только кадры, промпт и кит с картинкой")
    ap.add_argument("--no-send", action="store_true", help="не отправлять в Telegram")
    ap.add_argument("--keep-temp", action="store_true", help="не удалять промежуточные _seg*.mp4")
    ap.add_argument("--no-ai-background", action="store_true",
                    help="не генерировать AI-фон под матч, даже если AI_BACKGROUND=1 "
                         "(для бесплатных тестов) — взять статичный TABLE_IMAGE")
    ap.add_argument("--fresh-background", action="store_true",
                    help="перегенерировать AI-фон, даже если out_dir/_bg.jpg уже есть")
    args = ap.parse_args()

    if args.serve:
        import kit_server  # лениво: kit_server тянет http.server, остальным режимам не нужен
        try:
            kit_server.serve(args, run)
        except RuntimeError as e:
            log.error("%s", e)
            sys.exit(1)
        except KeyboardInterrupt:
            log.info("serve: остановлен")
        return
    if args.watch:
        try:
            watch(args)
        except KeyboardInterrupt:
            log.info("watch: остановлен")
        return
    if args.auto:
        try:
            matches = fixtures.fetch(
                days_ahead=env_int("FIXTURES_DAYS_AHEAD", 10, lo=1, hi=90),
                per_run=env_int("FIXTURES_PER_RUN", 1, lo=1, hi=20),
            )
        except fixtures.NoFixturesFound as e:
            # Пауза в календаре — нормальное состояние для крон-джобы, а не сбой:
            # выходим чисто (код 0), чтобы Railway не решил, что контейнер упал,
            # и не ушёл в рестарт-луп, долбящий API по кругу.
            log.warning("%s", e)
            sys.exit(0)
        except Exception as e:
            log.error("Не удалось получить расписание: %s", e)
            sys.exit(1)
    elif args.kit:
        matches = []
        for path in args.kit:
            try:
                matches.append(kit_source.match_from_kit(path))
            except kit_source.KitError as e:
                log.error("Не разобрал кит %s: %s", path, e)
        if not matches:
            sys.exit(1)
    elif args.sets_vertical:
        try:
            matches = coinplay_sets.fetch(
                args.sets_vertical,
                per_run=env_int("FIXTURES_PER_RUN", 1, lo=1, hi=20),
            )
        except coinplay_sets.NoSetsFound as e:
            # Как и NoFixturesFound у --auto: нормальная пауза для крон-джобы.
            log.warning("%s", e)
            sys.exit(0)
        except Exception as e:
            log.error("Не удалось получить наборы CoinPlay AI (%s): %s", args.sets_vertical, e)
            sys.exit(0)
    elif args.caption_file:
        if not args.caption_vertical:
            ap.error("--caption-file требует --caption-vertical")
        try:
            match_id = os.path.splitext(os.path.basename(args.caption_file))[0]
            matches = [telegram_caption.match_from_file(
                args.caption_file, args.caption_vertical, match_id=match_id)]
        except (OSError, telegram_caption.CaptionParseError) as e:
            log.error("Не разобрал %s: %s", args.caption_file, e)
            sys.exit(0)  # как и NoSetsFound — пустой/битый пост не повод падать крон-джобой
    elif args.match_file:
        matches = _load_match_file(args.match_file)
    elif args.home and args.away:
        matches = [{"home": args.home, "away": args.away, "home_flag": args.home_flag,
                    "away_flag": args.away_flag, "competition": args.competition,
                    "date": args.date, "scores": args.scores}]
    else:
        ap.error("нужны --serve, --watch, --auto, --kit, --sets-vertical, --caption-file, --home/--away или --match-file")

    done, failed = [], []
    for mt in matches:
        mt.setdefault("competition", "")
        mt.setdefault("date", "")
        label = f'{mt.get("home")} vs {mt.get("away")}'
        try:
            run(mt, args)
            done.append(label)
            if mt.get("id"):  # только у матчей из --auto (fixtures.py) — иначе нечего отмечать
                state.mark_posted(str(mt["id"]), competition=mt.get("competition", ""))
        except Exception as e:
            failed.append(label)
            log.exception("Матч %s не собран: %s", label, e)

    log.info("Итог: собрано %d, с ошибкой %d%s", len(done), len(failed),
             (" — " + ", ".join(failed)) if failed else "")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
