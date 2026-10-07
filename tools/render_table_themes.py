"""Строит assets/tables/<vertical>.jpg — тематические подложки под устройство
для каждой вертикали (football/esports/ufc), процедурно (PIL + numpy, без
внешних файлов/сервисов — в этом окружении нет доступа к AI-генерации картинок).

Палитра и общий язык — те же, что и у poster.py (тёмная поверхность + мятно-
зелёный неон #5EE08A + фиолетовые подсветы), чтобы устройство на фоне не
выбивалось по стилю независимо от вертикали. Средняя треть кадра по ширине
оставлена тёмной и малодетальной — туда composе_frame() кладёт устройство.

Запуск разово (не часть пайплайна генерации видео):
    python tools/render_table_themes.py
"""

from __future__ import annotations

import math
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "assets", "tables")

W, H = 1080, 1920

BG_TOP = (9, 8, 14)
BG_BOT = (18, 14, 26)
GOLD = (94, 224, 138)      # #5EE08A — тот же мятный неон, что и на карточке
VIOLET = (124, 88, 240)    # #7C58F0
LILAC = (186, 160, 255)
DIM = (46, 42, 58)


def _gradient(w: int, h: int, top, bottom) -> Image.Image:
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    top_a, bot_a = np.array(top, np.float32), np.array(bottom, np.float32)
    arr = np.repeat(top_a * (1 - t) + bot_a * t, w, axis=1)
    return Image.fromarray(arr.clip(0, 255).astype("uint8")).convert("RGBA")


def _brushed_noise(img: Image.Image, seed: int, amount: float = 6.0) -> Image.Image:
    """Лёгкая анизотропная зернистость — читается как шлифованный металл,
    не плоская заливка."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]
    lines = rng.normal(0, amount, (h, 1)).astype(np.float32)
    arr += np.repeat(lines, w, axis=1)[..., None]
    arr += rng.normal(0, amount * 0.4, (h, w))[..., None]
    out = Image.fromarray(arr.clip(0, 255).astype("uint8")).convert("RGBA")
    out.putalpha(img.split()[-1])
    return out


def _glow(canvas: Image.Image, draw_fn, color, alpha: int, blur: int) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def _ring(d: ImageDraw.ImageDraw, cx, cy, r, color, alpha, width):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color + (alpha,), width=width)


def _keep_center_clear(cx0: int, cx1: int) -> bool:
    """Силуэты держим вне центральной трети по X — туда ляжет устройство."""
    def inside(x):
        return cx0 <= x <= cx1
    return inside


CENTER_X0, CENTER_X1 = int(W * 0.30), int(W * 0.70)


def _edge_x(side: str, frac: float) -> int:
    """Координата X где-то у левого/правого края, никогда в центральной трети."""
    if side == "l":
        return int(W * 0.04 + frac * (CENTER_X0 - W * 0.04))
    return int(CENTER_X1 + frac * (W * 0.96 - CENTER_X1))


def _base(seed: int) -> Image.Image:
    base = _gradient(W, H, BG_TOP, BG_BOT)
    base = _brushed_noise(base, seed)
    # Угловые подсветы — тот же приём, что в poster._background()
    _glow(base, lambda d: d.ellipse(
        [-W * 0.3, -H * 0.05, W * 0.5, H * 0.22], fill=GOLD + (22,)), GOLD, 22, 140)
    _glow(base, lambda d: d.ellipse(
        [W * 0.55, H * 0.75, W * 1.3, H * 1.05], fill=VIOLET + (26,)), VIOLET, 26, 160)
    return base


def _coin(d: ImageDraw.ImageDraw, cx, cy, r, rng):
    """Монета-бейдж — общий crypto-casino акцент на всех трёх фонах."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(26, 22, 16, 235),
              outline=GOLD + (230,), width=max(2, r // 14))
    d.ellipse([cx - r * .72, cy - r * .72, cx + r * .72, cy + r * .72],
              outline=GOLD + (120,), width=max(1, r // 22))
    # символ "B" упрощённо двумя дугами
    d.line([cx - r * .12, cy - r * .42, cx - r * .12, cy + r * .42], fill=GOLD + (230,), width=max(2, r // 10))
    for dy in (-r * .2, r * .2):
        d.arc([cx - r * .12, cy + dy - r * .22, cx + r * .32, cy + dy + r * .22],
              -90, 90, fill=GOLD + (230,), width=max(2, r // 10))


def _chip_stack(d: ImageDraw.ImageDraw, cx, cy, r, n, rng):
    for i in range(n):
        yy = cy - i * r * 0.55
        d.ellipse([cx - r, yy - r * .32, cx + r, yy + r * .32],
                  fill=(18, 16, 14, 230), outline=GOLD + (200,), width=3)
        for a in range(0, 360, 45):
            rad = math.radians(a)
            d.line([cx + r * .78 * math.cos(rad), yy + r * .26 * math.sin(rad),
                   cx + r * .96 * math.cos(rad), yy + r * .32 * math.sin(rad)],
                  fill=VIOLET + (200,), width=3)


def _crypto_accents(canvas: Image.Image, seed: int) -> None:
    rng = random.Random(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _coin(d, _edge_x("l", 0.25), int(H * 0.14), 52, rng)
    _coin(d, _edge_x("r", 0.7), int(H * 0.86), 44, rng)
    _chip_stack(d, _edge_x("r", 0.3), int(H * 0.2), 46, 4, rng)
    _chip_stack(d, _edge_x("l", 0.6), int(H * 0.88), 40, 3, rng)
    canvas.alpha_composite(layer)


# --------------------------------------------------------------- football ---

def _football(canvas: Image.Image, seed: int) -> None:
    rng = random.Random(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # мяч — верхний левый край, кольцо + пятиугольная решётка неоновыми линиями
    bx, by, br = _edge_x("l", 0.3), int(H * 0.46), 110
    d.ellipse([bx - br, by - br, bx + br, by + br], outline=GOLD + (170,), width=5)
    for a in range(0, 360, 72):
        rad = math.radians(a)
        x0, y0 = bx + br * .5 * math.cos(rad), by + br * .5 * math.sin(rad)
        x1, y1 = bx + br * .95 * math.cos(rad), by + br * .95 * math.sin(rad)
        d.line([x0, y0, x1, y1], fill=GOLD + (120,), width=3)
    d.ellipse([bx - br * .4, by - br * .4, bx + br * .4, by + br * .4],
              outline=GOLD + (140,), width=3)

    # бутса — нижний правый край, простой угловатый силуэт
    fx, fy = _edge_x("r", 0.35), int(H * 0.62)
    boot = [(fx - 130, fy + 30), (fx - 90, fy - 40), (fx + 10, fy - 55),
           (fx + 120, fy - 20), (fx + 140, fy + 25), (fx + 110, fy + 55),
           (fx - 110, fy + 55)]
    d.line(boot + [boot[0]], fill=VIOLET + (180,), width=5)
    for i in range(3):  # шнуровка
        yy = fy - 30 + i * 18
        d.line([fx - 30, yy, fx + 30, yy - 8], fill=GOLD + (160,), width=3)

    # трибуны/прожекторы — размытые дуги в дальних углах
    for (cx, cy, r) in ((0, 0, W * 0.5), (W, int(H * 0.08), W * 0.4)):
        d.arc([cx - r, cy - r * .3, cx + r, cy + r * 1.4], 0, 360, fill=GOLD + (40,), width=6)

    # травинки у нижнего края
    for i in range(14):
        gx = int(W * 0.06 + i * (W * 0.88) / 13 + rng.uniform(-10, 10))
        if CENTER_X0 - 40 < gx < CENTER_X1 + 40:
            continue
        gh = rng.randint(26, 54)
        d.line([gx, H - 6, gx + rng.randint(-8, 8), H - 6 - gh], fill=VIOLET + (90,), width=3)

    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(0.6)))
    _glow(canvas, lambda dd: dd.ellipse([bx - br, by - br, bx + br, by + br], outline=GOLD + (90,), width=10),
         GOLD, 90, 18)


# ---------------------------------------------------------------- esports ---

def _esports(canvas: Image.Image, seed: int) -> None:
    rng = random.Random(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # клавиатура — нижний левый, сетка клавиш общим контуром (без брендинга)
    kx0, ky0 = _edge_x("l", 0.08), int(H * 0.74)
    kw, kh, cols, rows_n = 300, 150, 10, 5
    d.rounded_rectangle([kx0, ky0, kx0 + kw, ky0 + kh], radius=16,
                        outline=VIOLET + (180,), width=4)
    cw, ch = kw / cols, kh / rows_n
    for r in range(rows_n):
        for c in range(cols):
            if rng.random() < 0.12:
                continue
            x0, y0 = kx0 + c * cw + 3, ky0 + r * ch + 3
            lit = rng.random() < 0.3
            d.rounded_rectangle([x0, y0, x0 + cw - 6, y0 + ch - 6], radius=3,
                                fill=(GOLD + (160,)) if lit else (24, 22, 30, 200))

    # гарнитура — верхний правый, дуга + два наушника
    hx, hy = _edge_x("r", 0.4), int(H * 0.2)
    d.arc([hx - 120, hy - 140, hx + 120, hy + 40], 180, 360, fill=GOLD + (190,), width=8)
    for sx in (-1, 1):
        cxh = hx + sx * 118
        d.rounded_rectangle([cxh - 30, hy - 10, cxh + 30, hy + 90], radius=22,
                            outline=GOLD + (190,), width=6)

    # геймпад-стик — нижний правый, кольцо + крестовина
    gx, gy, gr = _edge_x("r", 0.3), int(H * 0.88), 70
    d.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], outline=VIOLET + (180,), width=6)
    d.ellipse([gx - gr * .4, gy - gr * .4, gx + gr * .4, gy + gr * .4], fill=(20, 18, 26, 220),
              outline=GOLD + (160,), width=3)

    # мышь — верхний левый
    mx, my = _edge_x("l", 0.35), int(H * 0.1)
    d.rounded_rectangle([mx - 55, my - 20, mx + 55, my + 110], radius=40,
                        outline=VIOLET + (170,), width=5)
    d.line([mx, my - 20, mx, my + 40], fill=GOLD + (140,), width=3)

    # дальние размытые полосы сценического света
    for i, cx in enumerate((W * -0.1, W * 1.1)):
        col = GOLD if i == 0 else VIOLET
        d.polygon([(cx, 0), (cx + (120 if i == 0 else -120), 0), (cx, H * .5), (cx - 40, H * .5)],
                  fill=col + (26,))

    canvas.alpha_composite(layer)
    _glow(canvas, lambda dd: dd.arc([hx - 120, hy - 140, hx + 120, hy + 40], 180, 360,
                                    fill=GOLD + (120,), width=14), GOLD, 120, 20)


# -------------------------------------------------------------------- ufc ---

def _ufc(canvas: Image.Image, seed: int) -> None:
    rng = random.Random(seed)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # восьмиугольник клетки — заметный контур в дальнем фоне, по центру кадра
    ocx, ocy, orad = W // 2, int(H * 0.46), int(W * 0.62)
    pts = [(ocx + orad * math.cos(math.radians(a)), ocy + orad * 0.78 * math.sin(math.radians(a)))
          for a in range(0, 360, 45)]
    d.polygon(pts, outline=VIOLET + (130,), width=5)
    for px, py in pts:  # стойки клетки — вертикальные штрихи в углах восьмиугольника
        d.line([px, py - 22, px, py + 22], fill=VIOLET + (100,), width=3)

    def _glove(gx, gy, r):
        """Перчатка: овальный кулак + отдельный круглый большой палец сбоку,
        манжета снизу — читается как перчатка, не как гантель."""
        d.ellipse([gx - r, gy - r * .85, gx + r, gy + r * .85],
                  outline=GOLD + (190,), width=6)
        d.ellipse([gx - r * 1.28, gy - r * .1, gx - r * .65, gy + r * .5],
                  outline=GOLD + (190,), width=5)  # большой палец
        d.rounded_rectangle([gx - r * .62, gy + r * .55, gx + r * .62, gy + r * 1.15],
                            radius=18, outline=GOLD + (170,), width=5)  # манжета
        for i in range(2):
            yy = gy + r * .72 + i * (r * .22)
            d.line([gx - r * .45, yy, gx + r * .45, yy], fill=VIOLET + (150,), width=3)

    # перчатка — нижний левый
    _glove(_edge_x("l", 0.3), int(H * 0.68), 78)
    # вторая перчатка — верхний правый, поменьше, зеркально
    _glove(_edge_x("r", 0.35), int(H * 0.15), 60)

    # пояс чемпиона — нижний правый, дуга с бляхами-ромбами
    bx, by, brad = _edge_x("r", 0.4), int(H * 0.9), 160
    d.arc([bx - brad, by - brad * .5, bx + brad, by + brad * .5], 200, 340,
         fill=GOLD + (170,), width=10)
    for t in (0.25, 0.5, 0.75):
        ang = math.radians(200 + t * 140)
        px = bx + brad * math.cos(ang)
        py = by + brad * .5 * math.sin(ang)
        s = 16
        d.polygon([(px, py - s), (px + s, py), (px, py + s), (px - s, py)], fill=VIOLET + (180,))

    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(0.5)))
    _glow(canvas, lambda dd: dd.polygon(pts, outline=VIOLET + (90,), width=10), VIOLET, 90, 22)


THEMES = {
    "football": (_football, 101),
    "esports": (_esports, 202),
    "ufc": (_ufc, 303),
}


def render(vertical: str) -> Image.Image:
    fn, seed = THEMES[vertical]
    canvas = _base(seed)
    _crypto_accents(canvas, seed)
    fn(canvas, seed)
    # лёгкая итоговая виньетка, как у карточки
    vign = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(vign).ellipse([-300, -200, W + 300, H + 300], fill=255)
    vign = vign.filter(ImageFilter.GaussianBlur(260))
    dark = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
    dark.putalpha(vign.point(lambda v: int((255 - v) * 0.28)))
    return Image.alpha_composite(canvas, dark).convert("RGB")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for vertical in THEMES:
        img = render(vertical)
        path = os.path.join(OUT_DIR, f"{vertical}.jpg")
        img.save(path, quality=92)
        print(f"{vertical}: {path} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
