"""
Telegram-группа → Threads: автопостинг.

Источник читается юзер-сессией Telethon (бот не видит сообщения других ботов).
Варианты одного материала схлопываются в один пост.
Медиа раздаётся наружу, потому что Threads скачивает его по URL сам.
"""

import logging
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import db
import instagram_api
import post_filter
import telegram_source
import threads_api
import worker
import x_api
from config import (
    ADMIN_TOKEN,
    DATA_DIR,
    INSTAGRAM_ENABLED,
    MEDIA_DIR,
    POST_FILTER_PHRASE,
    SELECT_STRATEGY,
    THREADS_ENABLED,
    X_ENABLED,
    X_SELECT_STRATEGY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()

    # Проверяем, что каталог для медиа доступен на запись. Без Volume
    # картинки скачать будет некуда, а посты уйдут без них.
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        probe = os.path.join(MEDIA_DIR, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        log.info("Каталог медиа доступен: %s", MEDIA_DIR)
    except Exception as e:
        log.error(
            "КАТАЛОГ МЕДИА НЕДОСТУПЕН (%s): %s. "
            "Проверь, что Volume примонтирован в %s — иначе посты уйдут без картинок.",
            MEDIA_DIR, e, DATA_DIR,
        )

    if THREADS_ENABLED:
        try:
            me = threads_api.whoami()
            log.info("Threads-аккаунт: %s (id %s)", me.get("username"), me.get("id"))
        except Exception as e:
            log.error("Не удалось проверить токен Threads: %s", e)
    else:
        log.info("Threads отключён (THREADS_ENABLED=false)")

    if INSTAGRAM_ENABLED:
        try:
            me = instagram_api.whoami()
            log.info("Instagram-аккаунт: @%s (id %s)",
                     me.get("username"), me.get("id"))
        except Exception as e:
            log.error("Не удалось проверить токен Instagram: %s", e)
    else:
        log.info("Instagram отключён (INSTAGRAM_ENABLED=false)")

    if X_ENABLED:
        try:
            me = x_api.whoami()
            log.info("X-аккаунт: @%s (id %s)", me.get("username"), me.get("id"))
        except Exception as e:
            log.error("Не удалось проверить ключи X: %s", e)
    else:
        log.info("X отключён (X_ENABLED=false)")

    if post_filter.ENABLED:
        log.info("Фильтр: публикуются только посты с %r", POST_FILTER_PHRASE)
    else:
        log.info("Фильтр выключен: публикуются все посты")
    log.info("Стратегия выбора: Threads=%s, X=%s", SELECT_STRATEGY, X_SELECT_STRATEGY)

    await telegram_source.start()
    worker.start()
    yield
    worker.stop()
    await telegram_source.stop()


app = FastAPI(title="TG → Threads autopost", lifespan=lifespan)


@app.get("/media/{key}")
def media(key: str):
    """Публичная ссылка на файл — по ней Threads забирает медиа."""
    entry = db.get_media(key)
    if not entry:
        raise HTTPException(status_code=404, detail="Не найдено")
    return FileResponse(entry["path"], media_type=entry["mime"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    return {"queue": db.stats(), "recent": db.recent_bursts(20)}


@app.get("/dialogs")
async def dialogs():
    """Список доступных чатов с их id — чтобы найти SOURCE_CHAT_ID."""
    try:
        return {"dialogs": await telegram_source.list_dialogs()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/requeue/{burst_id}")
def admin_requeue(burst_id: str, token: str = ""):
    """
    Ставит уже обработанную пачку обратно в очередь — воркер подхватит её в
    ближайший тик (WORKER_INTERVAL_SECONDS) и опубликует на те площадки,
    которых ещё нет в её 'results' (площадки, уже отмеченные успешными,
    повторно не публикуются — см. worker._process_burst).

    Нужен, например, чтобы прогнать старый пост в Instagram после того, как
    его включили уже после публикации в Threads/X.
    """
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="ADMIN_TOKEN не задан — /admin отключён. "
                   "Задайте ADMIN_TOKEN в переменных окружения, чтобы включить.",
        )
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Неверный token")

    burst = db.get_burst(burst_id)
    if not burst:
        raise HTTPException(status_code=404, detail="Пачка не найдена")

    db.requeue_burst(burst_id)
    log.info("Пачка %s поставлена в очередь вручную через /admin/requeue",
             burst_id[:8])
    return {
        "requeued": burst_id,
        "title": (burst.get("manifest") or "").splitlines()[0][:80],
        "already_posted_to": list(json.loads(burst.get("results") or "{}").keys()),
    }
