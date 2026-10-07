#!/usr/bin/env python3
"""Генерирует НЕПРИВЯЗАННЫЕ к конкретному матчу тематические фоны (рука +
лист на фоне стадиона/арены/октагона, без флагов/цветов команд) — на замену
старых процедурных assets/tables/<vertical>.jpg (сделанных под прежний
дизайн "AI-устройства") или как статичный запасной вариант, когда
AI_BACKGROUND=0 / сгенерировать фон под конкретный матч не вышло.

Если нужен фон С флагами/цветами команд матча — это делает generate.py сам
на каждый прогон через backgrounds.py (см. AI_BACKGROUND в README), а не
этот скрипт: он даёт только общие "дежурные" фоны без привязки к командам.

Запуск (там, где уже стоит OPENAI_API_KEY — например в консоли Railway
сервиса ai-match-lab):

    python tools/generate_backgrounds.py                  # все 3 вертикали, 3 варианта каждая
    python tools/generate_backgrounds.py --vertical football
    python tools/generate_backgrounds.py --vertical ufc --count 5

Файлы сохраняются в tools/bg_previews/<vertical>/<n>.jpg — посмотрите их и
скопируйте тот, что понравился, в assets/tables/<vertical>.jpg (так его
подхватит generate.py без изменений в коде, см. _VERTICAL_TABLE).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backgrounds  # noqa: E402

log = logging.getLogger("bg")
logging.basicConfig(level=logging.INFO, format="%(asctime)s bg: %(message)s")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "bg_previews")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vertical", choices=sorted(backgrounds.SCENES), default="",
                     help="только одна вертикаль (по умолчанию — все три)")
    ap.add_argument("--count", type=int, default=3,
                     help="сколько вариантов на вертикаль (по умолчанию 3)")
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        log.error("OPENAI_API_KEY не задан в окружении")
        return 1

    verticals = [args.vertical] if args.vertical else sorted(backgrounds.SCENES)
    total = ok = 0
    for vertical in verticals:
        vdir = os.path.join(args.out_dir, vertical)
        os.makedirs(vdir, exist_ok=True)
        for i in range(1, args.count + 1):
            total += 1
            out_path = os.path.join(vdir, f"{i}.jpg")
            # без home/away -> обобщённая сцена без флагов/цветов конкретных команд
            if backgrounds.generate_to_file(vertical, "", "", out_path, api_key):
                ok += 1
            time.sleep(1.5)  # не долбить API пачкой подряд

    log.info("Готово: %d/%d изображений. Смотрите %s, выбранный файл "
              "скопируйте в assets/tables/<vertical>.jpg", ok, total,
              args.out_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
