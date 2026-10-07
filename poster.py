"""Рендер бланка AI Match Lab: простой распечатанный листок Coinplay
(белая бумага, чёрные чернила) в руке на тематическом фоне, кадр 9:16 —
как на референсе: лого, команды, ОДИН крупный бокс с итоговым счётом,
полоска мини-иконок моделей, которые реально дали прогноз, и
"LINK IN BIO".

Два кадра на матч:
  * blank  — пустой бокс со счётом (первый кадр видео);
  * filled — бокс заполнен (последний кадр видео).
Видео-модель интерполирует между ними — цифры появляются в боксе, и в конце
на листе гарантированно стоит именно наш прогноз, а не то, что модель
«придумала» сама. Полоска иконок моделей статична в обоих кадрах — меняется
только сам бокс со счётом (см. video.build_prompt()).

render_paper() рисует сам листок (белый, портретный, см. PAPER_W/PAPER_H).
Высота листа зависит от того, есть ли реальная разбивка по моделям (больше
одной строки в Match.rows) — тогда под итоговым счётом добавляется полоска
их мини-иконок с индивидуальным пиком каждой; если данные только
агрегатные (см. telegram_caption.py), полоски нет и лист короче.

compose_frame() кладёт готовый лист на тематический фон (TABLE_IMAGE_
<VERTICAL> в generate.py, или AI-фон под конкретный матч — см.
backgrounds.py) с перспективой и светотенью.
"""

from __future__ import annotations

import functools
import math
import os
import random
import re
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "assets", "fonts")
ICONS = os.path.join(HERE, "assets", "icons")
LOGO_MARK = os.path.join(HERE, "assets", "logo", "mark.png")  # настоящий знак Coinplay

# Кадр — как у референса (720x1280), рендерим в 1080x1920.
FRAME_W, FRAME_H = 1080, 1920
# Холст листа — простой распечатанный А4-подобный лист (см. референс:
# белая бумага в руке на фоне стадиона/арены), а не вытянутый экран
# устройства. Высота БАЗОВОГО (без полоски моделей) листа; см. _canvas_h().
PAPER_W, PAPER_H = 1680, 1560

# --- простой печатный листок (по референсу: белая бумага, чёрные чернила,
# рука на тематическом фоне) ---
SHEET_BG = (250, 249, 246)     # тёплый белый, не ядовито-белый
SHEET_INK = (24, 22, 28)       # почти чёрный — основной текст/линии
SHEET_GREY = (120, 116, 128)   # второстепенный текст (лига, дата, подписи)
SHEET_LINE = (60, 56, 66)      # тонкие разделители/рамки в состоянии "до"
SHEET_ACCENT = (16, 128, 74)   # зелёный акцент бренда — рамка после заполнения, LINK IN BIO

# Нейтральная палитра для значков моделей (_glyph/_paste_icon) — не часть
# бумаги, рисуется поверх белых/цветных кружков иконок.
PANEL = (15, 13, 24)
GOLD = (94, 224, 138)
PRIMARY = GOLD
YELLOW = (150, 255, 189)
WHITE = (247, 247, 250)
LILAC = (186, 160, 255)


class AssetMissing(RuntimeError):
    pass


@functools.lru_cache(maxsize=64)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Шрифты кэшируются — подбор кегля (_fit_font) иначе создавал бы новый
    FreeTypeFont на каждой итерации перебора размеров."""
    path = os.path.join(FONTS, name)
    if not os.path.exists(path):
        raise AssetMissing(f"Нет файла шрифта {path} — проверь папку assets/fonts")
    return ImageFont.truetype(path, size)


@dataclass
class Row:
    name: str                      # "CHATGPT"
    home: int | None = None
    away: int | None = None
    icon: str = ""                 # ключ иконки: assets/icons/<icon>.png


@dataclass
class Match:
    home: str
    away: str
    home_flag: Image.Image
    away_flag: Image.Image
    rows: list = field(default_factory=list)
    title: str = "COINPLAY AI LAB"
    subtitle: str = "5 AI MODELS PREDICT"
    competition: str = ""
    date: str = ""
    consensus: str = ""       # кого выбрали модели: «SÃO PAULO FC»
    consensus_note: str = ""  # насколько единодушно: «4 OF 5 MODELS AGREE»
    consensus_pct: int = 0    # то же самое согласие числом: round(n / total * 100)
    vertical: str = ""        # football | esports | ufc — для иконки шапки листа
    # Счёт/победитель для ГЛАВНОГО бокса — консенсус/большинство моделей, а
    # НЕ обязательно rows[0] (если rows — реальная разбивка нескольких живых
    # моделей, они почти никогда не сходятся в одну точную строку). Когда не
    # заданы явно (None), render_paper() падает обратно на rows[0], как
    # раньше — так остаются рабочими старые прямые вызовы/тесты.
    hero_home: int | None = None
    hero_away: int | None = None


def _overlay(base: Image.Image, tile: Image.Image, xy: tuple[int, int]) -> None:
    """Корректное наложение RGBA поверх RGBA.

    `base.paste(tile, xy, tile)` смешивает и альфа-канал тоже, из-за чего
    у непрозрачной подложки в месте вставки альфа проседала ниже 255.
    alpha_composite делает то, что нужно.
    """
    if base.mode != "RGBA":
        base.paste(tile, xy, tile)
        return
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(tile, xy)
    base.alpha_composite(layer)


# ---------------------------------------------------------------- иконки ---

def _glyph(draw: ImageDraw.ImageDraw, key: str, cx: int, cy: int, r: int) -> None:
    """Нейтральные абстрактные значки, если своей иконки модели нет."""
    w = max(3, r // 7)
    col = WHITE
    if key == "chatgpt":        # шестигранный «узел»
        for i in range(6):
            a = math.radians(60 * i)
            b = math.radians(60 * i + 60)
            draw.line([(cx + r * .55 * math.cos(a), cy + r * .55 * math.sin(a)),
                       (cx + r * .55 * math.cos(b), cy + r * .55 * math.sin(b))],
                      fill=col, width=w)
        draw.ellipse([cx - r * .18, cy - r * .18, cx + r * .18, cy + r * .18], outline=col, width=w)
    elif key == "claude":       # лучистая звезда
        for i in range(12):
            a = math.radians(30 * i)
            draw.line([(cx + r * .15 * math.cos(a), cy + r * .15 * math.sin(a)),
                       (cx + r * .6 * math.cos(a), cy + r * .6 * math.sin(a))],
                      fill=YELLOW, width=w)
    elif key == "gemini":       # четырёхлучевая искра
        pts = []
        for i in range(8):
            a = math.radians(45 * i - 90)
            rr = r * .62 if i % 2 == 0 else r * .16
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        draw.polygon(pts, fill=LILAC)
    elif key == "perplexity":   # «решётка»
        s = r * .45
        draw.rectangle([cx - s, cy - s * .5, cx + s, cy + s * .5], outline=col, width=w)
        draw.line([(cx, cy - s * 1.2), (cx, cy + s * 1.2)], fill=col, width=w)
        draw.line([(cx - s, cy - s), (cx + s, cy + s)], fill=col, width=w)
        draw.line([(cx + s, cy - s), (cx - s, cy + s)], fill=col, width=w)
    else:                       # кольцо с диагональю
        draw.ellipse([cx - r * .45, cy - r * .45, cx + r * .45, cy + r * .45], outline=col, width=w)
        draw.line([(cx - r * .55, cy + r * .55), (cx + r * .55, cy - r * .55)], fill=col, width=w)


def _paste_icon(img: Image.Image, key: str, cx: int, cy: int, r: int) -> None:
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL, outline=PRIMARY, width=4)
    path = os.path.join(ICONS, f"{key}.png") if key else ""
    if path and os.path.exists(path):
        try:
            with Image.open(path) as raw:
                ic = raw.convert("RGBA")
            side = max(1, int(r * 1.3))
            ic.thumbnail((side, side), Image.LANCZOS)
            _overlay(img, ic, (cx - ic.width // 2, cy - ic.height // 2))
            return
        except OSError:
            pass  # битый PNG — не повод ронять весь рендер, рисуем глиф
    _glyph(d, key, cx, cy, r)


# ----------------------------------------------------------------- лист ----

def _text_c(d, xy, text, font, fill) -> None:
    d.text(xy, text, font=font, fill=fill, anchor="mm")


def _sheet_wash(w: int, h: int) -> Image.Image:
    """Тёплый белый с едва заметным наклоном в мятный книзу — вместо плоской
    заливки читается как настоящий премиальный картон, а не распечатка."""
    top = np.array(SHEET_BG, dtype=np.float32)
    bottom = np.array((240, 247, 242), dtype=np.float32)
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    row = top * (1 - t) + bottom * t
    arr = np.repeat(row, w, axis=1)
    return Image.fromarray(arr.clip(0, 255).astype("uint8"))


def _drop_shadow(img: Image.Image, box, radius: int, blur: int = 16,
                 opacity: int = 60, offset: tuple[int, int] = (0, 7)) -> None:
    """Мягкая тень под карточкой/боксом — лёгкий «приподнятый» вид вместо
    плоской аппликации. `img` должен быть RGBA."""
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]],
        radius=radius, fill=(20, 18, 24, opacity))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def _flag_card(img: Image.Image, flag: Image.Image, box) -> None:
    _drop_shadow(img, box, radius=22)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=22, fill=WHITE, outline=PRIMARY, width=6)
    pad = 20
    fw, fh = x1 - x0 - 2 * pad, y1 - y0 - 2 * pad
    if fw <= 0 or fh <= 0:
        return
    src = flag.convert("RGBA")
    if not src.width or not src.height:
        return
    if abs(src.width / src.height - fw / fh) > 0.25:
        # эмблема клуба, а не флаг — вписываем без растяжения на белом
        fl = Image.new("RGB", (fw, fh), WHITE)
        fitted = src.copy()
        fitted.thumbnail((max(1, fw - 20), max(1, fh - 20)), Image.LANCZOS)
        fl.paste(fitted, ((fw - fitted.width) // 2, (fh - fitted.height) // 2), fitted)
    else:
        white = Image.new("RGBA", src.size, (255, 255, 255, 255))
        fl = Image.alpha_composite(white, src).convert("RGB").resize((fw, fh), Image.LANCZOS)
    mask = Image.new("L", (fw, fh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, fw, fh], radius=12, fill=255)
    img.paste(fl, (x0 + pad, y0 + pad), mask)


def _fit_font(d: ImageDraw.ImageDraw, text: str, max_w: int,
              name: str, big: int, small: int) -> ImageFont.FreeTypeFont:
    """Подбирает кегль, пока строка не влезет — названия команд бывают длинные
    («Olympique de Marseille»), и жёсткий размер их обрезал бы."""
    for size in range(big, small - 1, -2):
        font = _font(name, size)
        if d.textlength(text, font=font) <= max_w:
            return font
    return _font(name, small)


# Геометрия верхней части листа: лого/шапка, лига+дата, эмблемы команд, VS,
# имена команд — общая для всех вертикалей.
META_Y = 552            # лига и дата
FLAG_TOP, FLAG_BOT = 636, 916
FLAG_MARGIN = 120
FLAG_W = 520
VS_Y = 776
NAMES_Y = 976            # подписи команд под эмблемами

TABLE_X0, TABLE_X1 = 70, PAPER_W - 70  # общие левая/правая границы контента


def _sheet_icon(d: ImageDraw.ImageDraw, vertical: str, cx: int, cy: int, r: int, color) -> None:
    """Простой line-art значок по вертикали — тем же языком, что и логотип
    на референсе (тонкий чёрный мяч): никаких брендов/персонажей, только
    абстрактная форма предмета вида спорта."""
    w = max(3, r // 9)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=w)
    if vertical == "esports":
        bw, bh = r * 1.1, r * 0.6
        d.rounded_rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2],
                            radius=bh * 0.4, outline=color, width=w)
        d.line([cx - bw * 0.3, cy, cx - bw * 0.1, cy], fill=color, width=w)
        d.line([cx - bw * 0.2, cy - bh * 0.18, cx - bw * 0.2, cy + bh * 0.18], fill=color, width=w)
        for dx in (0.12, 0.3):
            d.ellipse([cx + bw * dx - w, cy - w, cx + bw * dx + w, cy + w], fill=color)
    elif vertical == "ufc":
        d.ellipse([cx - r * 0.55, cy - r * 0.2, cx + r * 0.55, cy + r * 0.55],
                  outline=color, width=w)
        d.ellipse([cx - r * 0.3, cy - r * 0.75, cx + r * 0.15, cy - r * 0.2],
                  outline=color, width=max(2, w - 1))
    else:  # football (дефолт)
        pts = [(cx + r * 0.5 * math.cos(math.radians(a - 90)),
                cy + r * 0.5 * math.sin(math.radians(a - 90))) for a in range(0, 360, 72)]
        d.polygon(pts, outline=color, width=max(2, w - 1))
        for px, py in pts:
            d.line([cx, cy, px, py], fill=color, width=1)


def _headline(m: Match) -> str:
    """«5 TOP AI MODELS PREDICT». Число — столько моделей реально показано
    на листе в полоске (не сырое число голосов в данных: зритель его не
    увидит и проверить не сможет)."""
    n = len(_strip_rows(m))
    return f"{n} TOP AI MODELS PREDICT" if n >= 2 else "AI PREDICTION"


def _agree_text(m: Match) -> str:
    """Согласие моделей без счёта голосов: «ALL MODELS AGREE» или «80% OF
    MODELS AGREE». Сколько всего голосов было в исходных данных — внутренняя
    кухня, на листе её нет."""
    pct = m.consensus_pct
    if not pct:
        note = re.search(r"(\d+)\s+OF\s+(\d+)", (m.consensus_note or "").upper())
        if note and int(note.group(2)):
            pct = round(100 * int(note.group(1)) / int(note.group(2)))
    if pct >= 100:
        return "ALL MODELS AGREE"
    return f"{pct}% OF MODELS AGREE" if pct > 0 else ""


def _sheet_header(img: Image.Image, m: Match) -> None:
    cy = 150
    badge_r = 62
    _drop_shadow(img, [PAPER_W // 2 - badge_r, cy - badge_r, PAPER_W // 2 + badge_r, cy + badge_r],
                radius=badge_r, blur=12, opacity=50, offset=(0, 5))
    d = ImageDraw.Draw(img)
    d.ellipse([PAPER_W // 2 - badge_r, cy - badge_r, PAPER_W // 2 + badge_r, cy + badge_r],
             fill=SHEET_ACCENT)
    _sheet_icon(d, m.vertical or "football", PAPER_W // 2, cy, 44, WHITE)
    d.text((PAPER_W // 2, cy + 94), "COINPLAY.COM", font=_font("RobotoCondensed-Bold.ttf", 66),
           fill=SHEET_INK, anchor="mm")
    sub = {"football": "FOOTBALL", "esports": "ESPORTS", "ufc": "UFC"}.get(m.vertical, "PREDICTIONS")
    d.text((PAPER_W // 2, cy + 148), sub, font=_font("RobotoCondensed-SemiBold.ttf", 32),
           fill=SHEET_GREY, anchor="mm")
    d.line([(TABLE_X0, cy + 196), (TABLE_X1, cy + 196)], fill=SHEET_LINE, width=3)
    # Главный заголовок — кто и что предсказывает ("10 AI MODELS PREDICT"),
    # ниже — акцентная подпись "TODAY'S MATCH". Раньше тут был один мелкий
    # "TODAY'S MATCH" и пустое поле до строки с лигой: с ленты не было
    # понятно, что это прогноз именно ИИ-моделей.
    d.text((PAPER_W // 2, cy + 262), _headline(m),
           font=_font("RobotoCondensed-Bold.ttf", 82), fill=SHEET_INK, anchor="mm")
    d.text((PAPER_W // 2, cy + 352), "TODAY'S MATCH",
           font=_font("RobotoCondensed-Bold.ttf", 52), fill=SHEET_ACCENT, anchor="mm")


def _team_names_ink(img: Image.Image, m: Match) -> None:
    d = ImageDraw.Draw(img)
    home_cx = FLAG_MARGIN + FLAG_W // 2
    away_cx = PAPER_W - FLAG_MARGIN - FLAG_W // 2
    for name, cx in ((m.home, home_cx), (m.away, away_cx)):
        text = name.upper()
        font = _fit_font(d, text, FLAG_W - 40, "RobotoCondensed-Bold.ttf", 68, 30)
        d.text((cx, NAMES_Y), text, font=font, fill=SHEET_INK, anchor="mm")


# ------------------------------------------------------- бокс со счётом ----
# Бокс — главный фокус всего листа (это и есть "предикт"), поэтому крупнее
# прежнего (420x190 -> 560x240) и с мягким акцентным свечением позади —
# иначе на маленьком превью в ленте взгляд не цепляется именно за него
# среди заголовка, эмблем и полоски моделей (см. прямую жалобу).

SCORE_BOX_Y0 = 1112
SCORE_BOX_W, SCORE_BOX_H = 560, 240
SCORE_LABEL_Y = SCORE_BOX_Y0 - 58


def _score_spotlight(img: Image.Image) -> None:
    cx = PAPER_W // 2
    cy = SCORE_BOX_Y0 + SCORE_BOX_H // 2
    rx, ry = SCORE_BOX_W // 2 + 140, SCORE_BOX_H // 2 + 140
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=SHEET_ACCENT + (46,))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(110)))


def _score_label(img: Image.Image, vertical: str) -> None:
    label = "PREDICTED WINNER" if vertical == "ufc" else "PREDICTED SCORE"
    d = ImageDraw.Draw(img)
    d.text((PAPER_W // 2, SCORE_LABEL_Y), label,
           font=_font("RobotoCondensed-Bold.ttf", 44), fill=SHEET_ACCENT, anchor="mm")


def _score_box(img: Image.Image, home_val: int | None, away_val: int | None,
               filled: bool, seed: int, vertical: str = "",
               home_name: str = "", away_name: str = "") -> None:
    """Главный бокс — пустой до реплика, с почти-рукописным ответом после.

    Футбол/киберспорт: два числа через разделитель, как настоящий счёт.
    UFC: счёта как такового нет (см. coinplay_sets.rows_from_predictions —
    победитель кодируется как 1/0), поэтому вместо "1–0" пишем имя
    предсказанного победителя одной строкой — цифры тут были бы враньём.
    """
    d = ImageDraw.Draw(img)
    cx = PAPER_W // 2
    x0, y0 = cx - SCORE_BOX_W // 2, SCORE_BOX_Y0
    x1, y1 = cx + SCORE_BOX_W // 2, SCORE_BOX_Y0 + SCORE_BOX_H
    outline = SHEET_ACCENT if filled else SHEET_LINE
    # Непрозрачная белая заливка (не просто обводка) — чтобы тень под боксом
    # (см. _drop_shadow в render_paper) не просвечивала внутрь и не портила
    # белизну пустого бокса, которая нужна видео-модели как чистый холст.
    d.rounded_rectangle([x0, y0, x1, y1], radius=20, fill=WHITE, outline=outline, width=6)
    is_ufc = vertical == "ufc"
    if not is_ufc:
        mid_x = cx
        d.line([mid_x, y0 + 24, mid_x, y1 - 24], fill=outline, width=4)
    if not filled or home_val is None or away_val is None:
        return
    rnd = random.Random(f"{seed}:sheet")
    cy = (y0 + y1) // 2
    if is_ufc:
        winner = home_name if home_val > away_val else away_name
        text = (winner or "").upper()
        font = _fit_font(d, text, SCORE_BOX_W - 64, "Kalam-Bold.ttf", 108, 48)
        tile = Image.new("RGBA", (SCORE_BOX_W, SCORE_BOX_H), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((SCORE_BOX_W // 2, SCORE_BOX_H // 2), text, font=font,
                                  fill=SHEET_INK + (255,), anchor="mm")
        tile = tile.rotate(rnd.uniform(-2.5, 2.5), resample=Image.BICUBIC)
        tile = tile.filter(ImageFilter.GaussianBlur(0.5))
        _overlay(img, tile, (x0, y0))
        return
    f = _font("Kalam-Bold.ttf", 150)
    for off, val in ((-SCORE_BOX_W // 4, home_val), (SCORE_BOX_W // 4, away_val)):
        tile = Image.new("RGBA", (280, 280), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((140, 150), str(val), font=f, fill=SHEET_INK + (255,), anchor="mm")
        tile = tile.rotate(rnd.uniform(-5, 5), resample=Image.BICUBIC)
        tile = tile.filter(ImageFilter.GaussianBlur(0.5))
        _overlay(img, tile, (int(cx + off - 140), int(cy - 140)))


# ------------------------------------------------- полоска моделей-судей ---
# Первая версия рисовала иконки/подписи слишком мелко (радиус 42px на
# полотне 1680px — после уменьшения листа до 72% кадра 1080px это читалось
# как шум, не как текст). Укрупнили в ~1.8 раза, подписи сделали чёрными
# (не серыми) и жирными, плюс добавили карточку-панель под всей полоской,
# чтобы она читалась отдельным блоком, а не случайными значками на пустом
# поле — см. запрос «вообще не понятно что это предикт от других моделей».

STRIP_ICON_R = 58
STRIP_MAX = 5           # крупнее иконки -> меньше их влезает в ряд читаемо
STRIP_EXTRA_H = 305     # на сколько выше канвас листа, когда полоска есть


def _row_pick_label(row: Row, vertical: str, home: str, away: str) -> str:
    if vertical == "ufc":
        if row.home is None or row.away is None:
            return "—"
        name = home if row.home > row.away else away
        name = (name or "").strip()
        return (name[:8] + "…") if len(name) > 9 else (name or "—")
    if row.home is None or row.away is None:
        return "—"
    return f"{row.home}-{row.away}"


# Самые узнаваемые модели показываем первыми: на лист влезает 5, и зритель
# должен увидеть знакомые названия, а не случайные пять из списка.
_STRIP_PRIORITY = ("chatgpt", "gemini", "claude", "grok", "perplexity",
                   "deepseek", "llama", "qwen", "mistral", "kimi")


def _strip_rows(m: Match) -> list:
    rows = [r for r in m.rows if r.home is not None and r.away is not None]

    def rank(r: Row) -> int:
        key = (r.icon or r.name or "").strip().lower().replace(" ", "")
        return _STRIP_PRIORITY.index(key) if key in _STRIP_PRIORITY else len(_STRIP_PRIORITY)

    return sorted(rows, key=rank)[:STRIP_MAX]


def _model_strip(img: Image.Image, m: Match, top: int) -> int:
    """Полоска иконок моделей, которые реально дали прогноз, с их
    индивидуальным пиком под каждой — играем на том, что за консенсусом
    стоит несколько независимых ИИ, не пряча это за одним агрегированным
    боксом (см. запрос «обыграть разные модели»). Крупная подпись-заголовок
    "MODELS CONSULTED" и рамка-панель вокруг всей полоски — чтобы при первом
    взгляде было сразу понятно, что это за блок, а не мелкий шум под счётом.

    Статична в обоих кадрах — пробовали прятать пики за "?" до реплика и
    открывать вместе с главным боксом (как на референсе), но на РЕАЛЬНОЙ
    генерации (не в нашем превью, а в фактическом ролике с Railway) это
    дало на видео-модели ровно то, от чего уже предостерегали раньше:
    несколько одновременно меняющихся текстовых зон (бокс + consensus_note
    + 5 подписей в полоске + CTA-строка снизу) видео-модель не смогла
    аккуратно интерполировать — текст на переходных кадрах плыл и
    дублировался ("0--1", "EXACT SCCORE POSTED SOSTED"). Вернули полоску к
    статике — единственная анимируемая зона на листе снова только главный
    бокс со счётом, это и держит интригу, а полоска — лишь подтверждение.

    Возвращает Y нижней границы полоски — чтобы вызывающий код знал, где
    рисовать следующий блок.
    """
    rows = _strip_rows(m)
    n = len(rows)
    if n == 0:
        return top
    d = ImageDraw.Draw(img)
    # Сознательно тише и мельче заголовка над главным боксом ("PREDICTED
    # SCORE") — полоска модели это подпись-подтверждение, а не второй
    # фокус внимания: если оба заголовка одинаково жирные и чёрные, глаз
    # не понимает, что на листе главное (см. жалобу "взгляд не цепляется
    # на предикт").
    panel_top = top
    cy = panel_top + 40 + STRIP_ICON_R
    name_y = cy + STRIP_ICON_R + 46
    pick_y = name_y + 62
    panel_bottom = pick_y + 46
    _drop_shadow(img, [TABLE_X0, panel_top, TABLE_X1, panel_bottom], radius=24)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([TABLE_X0, panel_top, TABLE_X1, panel_bottom],
                        radius=24, fill=WHITE, outline=SHEET_LINE, width=3)

    slot_w = (TABLE_X1 - TABLE_X0) / n
    for i, row in enumerate(rows):
        cx = int(TABLE_X0 + slot_w * (i + 0.5))
        _paste_icon(img, row.icon, cx, cy, STRIP_ICON_R)
        d = ImageDraw.Draw(img)  # _paste_icon может подменить содержимое img
        label = _row_pick_label(row, m.vertical, m.home, m.away)
        # Название модели текстом — значки лабораторий узнаёт не каждый.
        name = (row.name or "AI").upper()
        nfont = _fit_font(d, name, slot_w - 20, "RobotoCondensed-Bold.ttf", 44, 26)
        d.text((cx, name_y), name, font=nfont, fill=SHEET_INK, anchor="mm")
        font = _fit_font(d, label, slot_w - 24, "RobotoCondensed-Bold.ttf", 54, 28)
        d.text((cx, pick_y), label, font=font, fill=SHEET_ACCENT, anchor="mm")
    return panel_bottom


def _canvas_h(m: Match) -> int:
    return PAPER_H + (STRIP_EXTRA_H if len(m.rows) > 1 else 0)


def render_paper(m: Match, filled_rows: int = 0, seed: int = 7) -> Image.Image:
    """Простой распечатанный листок (белая бумага, чёрные чернила) — как на
    референсе: лого + вертикаль, команды с эмблемами, ОДИН крупный бокс с
    итоговым счётом/победителем, под ним — полоска мини-иконок моделей,
    которые реально дали прогноз (если есть реальная разбивка, см.
    Match.rows), призыв "LINK IN BIO". Листок кладётся на тематический фон
    уже в compose_frame()."""
    canvas_h = _canvas_h(m)
    img = _sheet_wash(PAPER_W, canvas_h).convert("RGBA")
    ImageDraw.Draw(img).rectangle([0, 0, PAPER_W, 14], fill=SHEET_ACCENT)
    _sheet_header(img, m)

    meta = " · ".join(x for x in (m.competition, m.date) if x)
    if meta:
        d = ImageDraw.Draw(img)
        _text_c(d, (PAPER_W // 2, META_Y + 40), meta.upper(),
                _fit_font(d, meta.upper(), TABLE_X1 - TABLE_X0 - 40,
                          "RobotoCondensed-Bold.ttf", 46, 26), SHEET_INK)

    _flag_card(img, m.home_flag, (FLAG_MARGIN, FLAG_TOP, FLAG_MARGIN + FLAG_W, FLAG_BOT))
    _flag_card(img, m.away_flag,
              (PAPER_W - FLAG_MARGIN - FLAG_W, FLAG_TOP, PAPER_W - FLAG_MARGIN, FLAG_BOT))
    r = 100
    _drop_shadow(img, [PAPER_W // 2 - r, VS_Y - r, PAPER_W // 2 + r, VS_Y + r],
                radius=r, blur=14, opacity=55)
    d = ImageDraw.Draw(img)
    d.ellipse([PAPER_W // 2 - r, VS_Y - r, PAPER_W // 2 + r, VS_Y + r],
             fill=SHEET_ACCENT, outline=WHITE, width=6)
    _text_c(d, (PAPER_W // 2, VS_Y - 4), "VS", _font("RobotoCondensed-Bold.ttf", 86), WHITE)
    _team_names_ink(img, m)

    # Счёт в главном боксе — консенсус/большинство (m.hero_home/away,
    # посчитанные вызывающим кодом из ВСЕХ строк), а не произвольно rows[0];
    # явного hero нет — используем rows[0] как раньше (одиночный агрегатный
    # ряд из telegram_caption.py и прямые вызовы без hero).
    if m.hero_home is not None and m.hero_away is not None:
        hero_h, hero_a = m.hero_home, m.hero_away
    elif m.rows:
        hero_h, hero_a = m.rows[0].home, m.rows[0].away
    else:
        hero_h = hero_a = None
    filled = filled_rows >= 1 and hero_h is not None and hero_a is not None
    _score_spotlight(img)
    _score_label(img, m.vertical)
    _drop_shadow(img, [PAPER_W // 2 - SCORE_BOX_W // 2, SCORE_BOX_Y0,
                       PAPER_W // 2 + SCORE_BOX_W // 2, SCORE_BOX_Y0 + SCORE_BOX_H],
                radius=20, blur=16, opacity=55)
    _score_box(img, hero_h, hero_a, filled, seed, m.vertical, m.home, m.away)

    # consensus_note статичен в обоих кадрах — см. docstring _model_strip:
    # прятать его синхронно с боксом пробовали (интрига как на референсе),
    # но на реальной генерации это вместе с полоской моделей давало слишком
    # много одновременно меняющихся текстовых зон, и видео-модель плыла.
    # Главный (и единственный анимируемый) драматический момент — большой
    # бокс со счётом; всё остальное — статичный контекст вокруг него.
    cursor = SCORE_BOX_Y0 + SCORE_BOX_H + 56
    agree = _agree_text(m)
    if agree:
        d = ImageDraw.Draw(img)
        _text_c(d, (PAPER_W // 2, cursor), agree,
                _font("RobotoCondensed-Bold.ttf", 44), SHEET_ACCENT)

    if len(m.rows) > 1:
        _model_strip(img, m, cursor + 58)

    return _card_texture(img.convert("RGB"))


def _card_texture(img: Image.Image) -> Image.Image:
    """Лёгкое зерно премиального картона + мягкая виньетка."""
    arr = np.asarray(img).astype(np.float32)
    rng = np.random.default_rng(11)
    arr += rng.normal(0, 2.6, arr.shape[:2])[..., None]
    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    light = 1.0 - 0.05 * (xx / w) - 0.04 * (yy / h)
    arr *= light[..., None]
    return Image.fromarray(arr.clip(0, 255).astype("uint8"))


# ---------------------------------------------------------------- стол ----

def _procedural_wood(w: int, h: int, seed: int = 3) -> Image.Image:
    """Тёмная деревянная столешница без внешних файлов (если нет TABLE_IMAGE)."""
    rng = np.random.default_rng(seed)
    y = np.arange(h)[:, None].astype(np.float32)
    x = np.arange(w)[None, :].astype(np.float32)
    grain = np.zeros((h, w), np.float32)
    for k in range(6):
        freq = rng.uniform(0.004, 0.02)
        amp = rng.uniform(4, 30)
        phase = rng.uniform(0, 6.28)
        grain += np.sin(x * freq * (k + 1) * 0.4 + np.sin(y * 0.002 * (k + 1) + phase) * amp) * (1 / (k + 1))
    span = float(grain.max() - grain.min())
    grain = (grain - grain.min()) / span if span > 1e-6 else np.zeros_like(grain)
    noise = rng.normal(0, 1, (h, w)).astype(np.float32)
    base = np.array([70, 56, 58], np.float32)
    dark = np.array([38, 28, 34], np.float32)
    t = (grain * 0.75 + 0.25 * (noise * 0.15 + 0.5)).clip(0, 1)[..., None]
    arr = base * (1 - t) + dark * t
    return Image.fromarray(arr.clip(0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1.2))


def _perspective_coeffs(src_pts, dst_pts):
    """Коэффициенты для Image.PERSPECTIVE (PIL тянет ИЗ dst В src)."""
    rows = []
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts):
        rows.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        rows.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
    a = np.array(rows, dtype=np.float64)
    b = np.array(src_pts, dtype=np.float64).reshape(8)
    return np.linalg.solve(a, b)


def _keystone(sheet: Image.Image, shrink: float = 0.045) -> Image.Image:
    """Лёгкий наклон: верх уже низа, как у листа, снятого чуть сверху-спереди.

    Без этого карточка — идеально осевой прямоугольник поверх фотографии,
    снятой под углом, и глаз сразу читает её как наклейку.
    """
    w, h = sheet.size
    dx = w * shrink
    dst = [(dx, 0), (w - dx, 0), (w, h), (0, h)]          # трапеция в кадре
    src = [(0, 0), (w, 0), (w, h), (0, h)]                 # исходный прямоугольник
    return sheet.transform((w, h), Image.PERSPECTIVE, _perspective_coeffs(src, dst),
                           resample=Image.BICUBIC)


def _relight(sheet: Image.Image, bg: Image.Image, x: int, y: int) -> Image.Image:
    """Переносит светотень сцены на карточку.

    Главная причина эффекта «наложено»: карточка освещена ровно, а фон —
    с направленным светом и почти чёрным центром. Берём яркость фона под
    карточкой, сильно размываем (остаётся только градиент света, без деталей)
    и умножаем на него карточку. Тогда она темнеет там же, где темнеет стол.
    """
    w, h = sheet.size
    patch = bg.convert("L").crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(90))
    lum = np.asarray(patch, dtype=np.float32) / 255.0
    mean = float(lum.mean()) or 1.0
    # Нормируем вокруг среднего: важна ФОРМА градиента, а не абсолютная
    # темнота фона — иначе на чёрном столе карточка стала бы нечитаемой.
    gain = np.clip(0.88 + 0.34 * (lum - mean) / max(mean, 0.05), 0.74, 1.14)[..., None]
    arr = np.asarray(sheet, dtype=np.float32)
    arr[..., :3] = np.clip(arr[..., :3] * gain, 0, 255)
    return Image.fromarray(arr.astype("uint8"))


def _match_grain(sheet: Image.Image, seed: int, amount: float = 5.0) -> Image.Image:
    """Немного зерна: фотофон шумит, идеально чистая карточка выдаёт себя."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(sheet, dtype=np.float32)
    noise = rng.normal(0.0, amount, arr.shape[:2])[..., None]
    arr[..., :3] = np.clip(arr[..., :3] + noise, 0, 255)
    return Image.fromarray(arr.astype("uint8"))


def compose_frame(paper: Image.Image, table_image: str = "", seed: int = 3,
                  progress: float = 1.0) -> Image.Image:
    """Кладёт распечатанный листок на тематический фон: перспектива,
    светотень сцены и контактная тень — как на референсе (простой листок,
    без устройства/корпуса вокруг него).

    `progress` оставлен в сигнатуре ради обратной совместимости с
    generate.py (раньше управлял яркостью LED корпуса устройства); для
    простого листка у него нет визуального эффекта.
    """
    bg = None
    if table_image and os.path.exists(table_image):
        try:
            with Image.open(table_image) as raw:
                src = raw.convert("RGB")
            scale = max(FRAME_W / src.width, FRAME_H / src.height)
            src = src.resize((int(src.width * scale) + 1, int(src.height * scale) + 1), Image.LANCZOS)
            left, top = (src.width - FRAME_W) // 2, (src.height - FRAME_H) // 2
            bg = src.crop((left, top, left + FRAME_W, top + FRAME_H))
        except OSError as e:
            # битый/нечитаемый TABLE_IMAGE не должен ронять прогон
            import logging
            logging.getLogger("poster").warning("Не открыл %s (%s) — рисую дерево", table_image, e)
    if bg is None:
        bg = _procedural_wood(FRAME_W, FRAME_H, seed)

    # Лист занимает ~76% ширины кадра — крупнее, чем раньше (0.72): на
    # телефонном превью прогноз должен читаться с первого взгляда, а не
    # тонуть в фоне (см. прямую жалобу «взгляд не цепляется на предикт»).
    sheet = paper.convert("RGBA")
    target_w = int(FRAME_W * 0.76)
    target_h = int(target_w * sheet.height / sheet.width)
    sheet = sheet.resize((target_w, target_h), Image.LANCZOS)

    # Наклон и перспектива держим едва заметными — раньше (shrink=0.035,
    # rotate=-1.1) лист на реальной фотосцене читался как небрежно брошенный
    # /criво лежащий, а не уверенно предъявленный камере (прямая жалоба
    # «листок лежит как-то криво»). Небольшой наклон всё ещё нужен — иначе
    # идеально осевой прямоугольник поверх фото под углом выглядит как
    # наклейка, — но едва на грани заметного, а не бросающийся в глаза.
    sheet = _keystone(sheet, shrink=0.015)
    sheet = sheet.rotate(-0.4, resample=Image.BICUBIC, expand=True)

    x = (FRAME_W - sheet.width) // 2
    y = (FRAME_H - sheet.height) // 2

    lit = _match_grain(_relight(sheet, bg, x, y), seed)

    mask = lit.split()[-1]
    out = bg.convert("RGBA")
    for offset, blur, opacity in (((8, 14), 50, 110), ((3, 6), 12, 150)):
        layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        layer.paste((0, 0, 0, 255), (x + offset[0], y + offset[1]),
                    mask.point(lambda a, o=opacity: o if a else 0))
        out = Image.alpha_composite(out, layer.filter(ImageFilter.GaussianBlur(blur)))
    out.alpha_composite(lit, (x, y))
    # лёгкая виньетка/неравномерный свет — ближе к фото со смартфона
    vign = Image.new("L", out.size, 0)
    ImageDraw.Draw(vign).ellipse([-300, -200, FRAME_W + 300, FRAME_H + 300], fill=255)
    vign = vign.filter(ImageFilter.GaussianBlur(220))
    dark = Image.new("RGBA", out.size, (0, 0, 0, 255))
    dark.putalpha(vign.point(lambda v: int((255 - v) * 0.22)))
    result = Image.alpha_composite(out, dark)
    # Бейдж с логотипом — отдельно от листа, в верхнем углу самого КАДРА
    # (см. запрос «добавляй логотип в углу каждого видео сверху»). Рисуем
    # его ПОСЛЕ виньетки, а не до — иначе затемнение по краям кадра съедало
    # бы именно его, ровно там, где он стоит.
    _brand_watermark(result)
    return result.convert("RGB")


def _brand_watermark(frame: Image.Image) -> None:
    """Белый знак Coinplay (assets/logo/mark.png — уже лежал в репозитории,
    просто нигде не был подключён) в кружке с полупрозрачной тёмной
    подложкой — подложка нужна, чтобы бейдж не терялся на светлых участках
    сгенерированного фона (прожекторы, яркое небо трибун), а не только на
    тёмном. Верхний правый угол, с мягкой тенью — тот же язык, что и у
    остальных «приподнятых» карточек на листе."""
    if not os.path.exists(LOGO_MARK):
        return
    r = 70
    margin = 44
    cx, cy = FRAME_W - margin - r, margin + r

    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, 120))
    frame.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))

    backdrop = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    ImageDraw.Draw(backdrop).ellipse([cx - r, cy - r, cx + r, cy + r], fill=(12, 12, 16, 150))
    frame.alpha_composite(backdrop)

    mark = Image.open(LOGO_MARK).convert("RGBA")
    d = int(r * 1.3)
    mark = mark.resize((d, d), Image.LANCZOS)
    frame.alpha_composite(mark, (cx - d // 2, cy - d // 2))
