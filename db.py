"""SQLite: пачки сообщений, очередь публикации, реестр медиа, состояние токена."""

import json
import os
import sqlite3
import threading
import time
import uuid

from config import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS bursts (
    id            TEXT PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    manifest      TEXT NOT NULL DEFAULT '',
    candidates    TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'open',
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    threads_ids   TEXT,
    kit           TEXT,
    results       TEXT NOT NULL DEFAULT '{}',
    publish_after REAL NOT NULL DEFAULT 0,
    last_msg_at   REAL NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bursts_status ON bursts(status, publish_after);
CREATE INDEX IF NOT EXISTS idx_bursts_chat ON bursts(chat_id, status, last_msg_at);

CREATE TABLE IF NOT EXISTS seen_messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS published_matches (
    match_key  TEXT NOT NULL,
    ptype      TEXT NOT NULL,
    platform   TEXT NOT NULL,
    burst_id   TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (match_key, ptype, platform)
);

CREATE TABLE IF NOT EXISTS media (
    key        TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    mime       TEXT NOT NULL DEFAULT 'application/octet-stream',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_conn = _connect()


def _migrate():
    """
    Дописывает колонки, появившиеся в новых версиях.
    CREATE TABLE IF NOT EXISTS существующую таблицу не меняет, поэтому база
    со старой схемой на Volume переживает деплой и ломает вставки.
    """
    expected = {
        "bursts": {
            "manifest": "TEXT NOT NULL DEFAULT ''",
            "results": "TEXT NOT NULL DEFAULT '{}'",
            "threads_ids": "TEXT",
            "error": "TEXT",
            "kit": "TEXT",
        },
        "media": {
            "mime": "TEXT NOT NULL DEFAULT 'application/octet-stream'",
        },
    }

    for table, columns in expected.items():
        try:
            rows = _conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            continue
        if not rows:
            continue

        existing = {r["name"] for r in rows}
        for name, ddl in columns.items():
            if name not in existing:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                print(f"[db] Добавлена колонка {table}.{name}")
    _conn.commit()


def init():
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()
        _migrate()


# --- Дедупликация входящих сообщений ---

def mark_seen(chat_id: int, message_id: int) -> bool:
    """True, если сообщение новое. False, если уже обрабатывали."""
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO seen_messages (chat_id, message_id, created_at) VALUES (?, ?, ?)",
                (chat_id, message_id, time.time()),
            )
            _conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


# --- Дедупликация по матчу ---

def claim_match(match_key: str, ptype: str, platform: str, burst_id: str,
                window_seconds: float) -> bool:
    """
    Занимает (матч, тип поста, площадка) за пачкой. True — можно публиковать:
    ключ был свободен, просроченный (другой матч тех же команд) либо уже
    принадлежит этой же пачке (повтор после ошибки). False — этот пост уже
    опубликован другой пачкой.
    """
    now = time.time()
    with _lock:
        row = _conn.execute(
            "SELECT burst_id, created_at FROM published_matches"
            " WHERE match_key = ? AND ptype = ? AND platform = ?",
            (match_key, ptype, platform),
        ).fetchone()
        if row is None:
            _conn.execute(
                "INSERT INTO published_matches (match_key, ptype, platform, burst_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (match_key, ptype, platform, burst_id, now),
            )
            _conn.commit()
            return True
        if row["burst_id"] == burst_id:
            return True
        if now - row["created_at"] >= window_seconds:
            _conn.execute(
                "UPDATE published_matches SET burst_id = ?, created_at = ?"
                " WHERE match_key = ? AND ptype = ? AND platform = ?",
                (burst_id, now, match_key, ptype, platform),
            )
            _conn.commit()
            return True
        return False


def release_match(match_key: str, ptype: str, platform: str, burst_id: str):
    """Отдаёт ключ обратно, если публикация не состоялась (ошибка или нечего публиковать)."""
    with _lock:
        _conn.execute(
            "DELETE FROM published_matches"
            " WHERE match_key = ? AND ptype = ? AND platform = ? AND burst_id = ?",
            (match_key, ptype, platform, burst_id),
        )
        _conn.commit()


def defer_burst(burst_id: str, publish_after: float):
    """Откладывает пачку: часть площадок ждёт своего времени (см. DEDUP_*_GRACE_SECONDS)."""
    now = time.time()
    with _lock:
        _conn.execute(
            "UPDATE bursts SET status = 'pending', publish_after = ?, updated_at = ?"
            " WHERE id = ?",
            (publish_after, now, burst_id),
        )
        _conn.commit()


# --- Пачки ---

def close_open_bursts(chat_id: int):
    """
    Закрывает открытые пачки чата: пришёл новый манифест, значит предыдущий
    пост собран полностью и его можно публиковать не дожидаясь таймаута.
    """
    now = time.time()
    with _lock:
        _conn.execute(
            "UPDATE bursts SET status = 'pending', publish_after = ?, updated_at = ?"
            " WHERE chat_id = ? AND status = 'open'",
            (now, now, chat_id),
        )
        _conn.commit()


def start_burst(chat_id: int, manifest: str, burst_wait: float) -> str:
    """Открывает новую пачку от манифеста."""
    now = time.time()
    burst_id = uuid.uuid4().hex
    with _lock:
        _conn.execute(
            "INSERT INTO bursts (id, chat_id, manifest, candidates, status,"
            " publish_after, last_msg_at, created_at, updated_at)"
            " VALUES (?, ?, ?, '[]', 'open', ?, ?, ?, ?)",
            (burst_id, chat_id, manifest, now + burst_wait, now, now, now),
        )
        _conn.commit()
    return burst_id


def add_candidate(chat_id: int, candidate: dict, burst_window: float,
                  burst_wait: float, grouped_id=None) -> str:
    """
    Кладёт сообщение в открытую пачку чата либо создаёт новую без манифеста
    (запасной путь, если источник прислал текст без манифеста).
    """
    now = time.time()
    with _lock:
        row = _conn.execute(
            "SELECT * FROM bursts WHERE chat_id = ? AND status = 'open'"
            " AND last_msg_at > ? ORDER BY created_at DESC LIMIT 1",
            (chat_id, now - burst_window),
        ).fetchone()

        if row:
            candidates = json.loads(row["candidates"])

            if grouped_id is not None:
                for c in candidates:
                    if c.get("grouped_id") == grouped_id:
                        c["media"].extend(candidate["media"])
                        if not c.get("text"):
                            c["text"] = candidate.get("text", "")
                        _conn.execute(
                            "UPDATE bursts SET candidates = ?, last_msg_at = ?,"
                            " publish_after = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(candidates), now, now + burst_wait, now, row["id"]),
                        )
                        _conn.commit()
                        return row["id"]

            candidates.append(candidate)
            _conn.execute(
                "UPDATE bursts SET candidates = ?, last_msg_at = ?,"
                " publish_after = ?, updated_at = ? WHERE id = ?",
                (json.dumps(candidates), now, now + burst_wait, now, row["id"]),
            )
            _conn.commit()
            return row["id"]

        burst_id = uuid.uuid4().hex
        _conn.execute(
            "INSERT INTO bursts (id, chat_id, manifest, candidates, status,"
            " publish_after, last_msg_at, created_at, updated_at)"
            " VALUES (?, ?, '', ?, 'open', ?, ?, ?, ?)",
            (burst_id, chat_id, json.dumps([candidate]), now + burst_wait, now, now, now),
        )
        _conn.commit()
        return burst_id


def start_zip_burst(chat_id: int, header: str, kit_payload: dict) -> str:
    """
    Открывает пачку из готового zip-набора (kit.json + картинки + тексты по
    площадкам уже разложены заранее). В отличие от start_burst(), тут нечего
    ждать — весь пост уже собран целиком, поэтому publish_after=сейчас и
    статус сразу 'pending': воркер заберёт её в ближайший тик.
    """
    now = time.time()
    burst_id = uuid.uuid4().hex
    with _lock:
        _conn.execute(
            "INSERT INTO bursts (id, chat_id, manifest, candidates, kit, status,"
            " publish_after, last_msg_at, created_at, updated_at)"
            " VALUES (?, ?, ?, '[]', ?, 'pending', ?, ?, ?, ?)",
            (burst_id, chat_id, header, json.dumps(kit_payload), now, now, now, now),
        )
        _conn.commit()
    return burst_id


def claim_ready_bursts(limit: int = 5) -> list:
    """Забирает созревшие пачки и помечает их processing."""
    now = time.time()
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM bursts WHERE status IN ('open', 'pending')"
            " AND publish_after <= ? ORDER BY created_at LIMIT ?",
            (now, limit),
        ).fetchall()
        if rows:
            _conn.executemany(
                "UPDATE bursts SET status = 'processing', updated_at = ? WHERE id = ?",
                [(now, r["id"]) for r in rows],
            )
            _conn.commit()
        return [dict(r) for r in rows]


def get_results(burst_id: str) -> dict:
    """Что уже опубликовано по площадкам: {'threads': [ids], 'x': [ids]}."""
    with _lock:
        row = _conn.execute(
            "SELECT results FROM bursts WHERE id = ?", (burst_id,)
        ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["results"] or "{}")
    except Exception:
        return {}


def save_result(burst_id: str, platform: str, ids: list):
    """
    Фиксирует успех по одной площадке. Нужно, чтобы при повторе не публиковать
    заново туда, где уже всё прошло.
    """
    now = time.time()
    with _lock:
        row = _conn.execute(
            "SELECT results FROM bursts WHERE id = ?", (burst_id,)
        ).fetchone()
        try:
            results = json.loads(row["results"] or "{}") if row else {}
        except Exception:
            results = {}
        results[platform] = ids
        _conn.execute(
            "UPDATE bursts SET results = ?, updated_at = ? WHERE id = ?",
            (json.dumps(results), now, burst_id),
        )
        _conn.commit()


def mark_posted(burst_id: str, threads_ids: list = None):
    now = time.time()
    with _lock:
        _conn.execute(
            "UPDATE bursts SET status = 'posted', threads_ids = ?, error = NULL,"
            " updated_at = ? WHERE id = ?",
            (json.dumps(threads_ids or []), now, burst_id),
        )
        _conn.commit()


def get_burst(burst_id: str):
    """Одна пачка по id — для ручного requeue из /admin."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM bursts WHERE id = ?", (burst_id,)
        ).fetchone()
    return dict(row) if row else None


def requeue_burst(burst_id: str) -> bool:
    """
    Возвращает уже обработанную (posted/skipped/failed) пачку в очередь на
    публикацию немедленно. Площадки, уже отмеченные в 'results', воркер не
    трогает повторно — уйдёт только то, что ещё не публиковалось (например,
    Instagram, включённый уже после того, как остальное было опубликовано).
    """
    now = time.time()
    with _lock:
        cur = _conn.execute(
            "UPDATE bursts SET status = 'pending', publish_after = 0,"
            " attempts = 0, error = NULL, updated_at = ? WHERE id = ?",
            (now, burst_id),
        )
        _conn.commit()
        return cur.rowcount > 0


def reopen_burst(burst_id: str, publish_after: float):
    """
    Возвращает пачку в открытое состояние: манифест и картинки пришли,
    а текст ещё нет — ждём его, вместо того чтобы публиковать пустышку.
    Статус 'open' нужен, чтобы текст подклеился сюда, а не создал новую пачку.
    """
    now = time.time()
    with _lock:
        _conn.execute(
            "UPDATE bursts SET status = 'open', publish_after = ?, updated_at = ?"
            " WHERE id = ?",
            (publish_after, now, burst_id),
        )
        _conn.commit()


def mark_skipped(burst_id: str, reason: str):
    now = time.time()
    with _lock:
        _conn.execute(
            "UPDATE bursts SET status = 'skipped', error = ?, updated_at = ? WHERE id = ?",
            (reason[:200], now, burst_id),
        )
        _conn.commit()


def mark_retry(burst_id: str, error: str, retry_after: float, max_attempts: int):
    now = time.time()
    with _lock:
        row = _conn.execute("SELECT attempts FROM bursts WHERE id = ?", (burst_id,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        status = "pending" if attempts < max_attempts else "failed"
        _conn.execute(
            "UPDATE bursts SET status = ?, attempts = ?, error = ?, publish_after = ?,"
            " updated_at = ? WHERE id = ?",
            (status, attempts, error[:1000], retry_after, now, burst_id),
        )
        _conn.commit()


def requeue_stuck(older_than_seconds: int = 900):
    """Возвращает в очередь пачки, зависшие в processing после рестарта."""
    cutoff = time.time() - older_than_seconds
    with _lock:
        _conn.execute(
            "UPDATE bursts SET status = 'pending' WHERE status = 'processing'"
            " AND updated_at < ?",
            (cutoff,),
        )
        _conn.commit()


def stats() -> dict:
    with _lock:
        rows = _conn.execute(
            "SELECT status, COUNT(*) AS n FROM bursts GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def recent_bursts(limit: int = 20) -> list:
    with _lock:
        rows = _conn.execute(
            "SELECT id, status, attempts, error, candidates, results, manifest,"
            " kit, created_at FROM bursts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        cands = json.loads(d.pop("candidates") or "[]")
        try:
            d["results"] = json.loads(d.get("results") or "{}")
        except Exception:
            d["results"] = {}
        manifest = d.pop("manifest", "") or ""
        kit_raw = d.pop("kit", None)
        kit = None
        if kit_raw:
            try:
                kit = json.loads(kit_raw)
            except Exception:
                kit = None
        if kit:
            d["title"] = manifest.splitlines()[0][:80] if manifest else "zip-набор"
            platforms = kit.get("platforms") or {}
            d["variants"] = len(platforms)
            first = next(iter(platforms.values()), {})
            d["preview"] = (first.get("text", "")[:80] if first else "")
        else:
            d["title"] = manifest.splitlines()[0][:80] if manifest else ""
            d["variants"] = len(cands)
            d["preview"] = (cands[0].get("text", "")[:80] if cands else "")
        out.append(d)
    return out


# --- Медиа ---

def register_media(path: str, kind: str, mime: str) -> str:
    key = uuid.uuid4().hex
    with _lock:
        _conn.execute(
            "INSERT INTO media (key, path, kind, mime, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, path, kind, mime, time.time()),
        )
        _conn.commit()
    return key


def get_media(key: str):
    with _lock:
        row = _conn.execute("SELECT * FROM media WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def old_media(older_than_seconds: int) -> list:
    cutoff = time.time() - older_than_seconds
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM media WHERE created_at < ?", (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_media(key: str):
    with _lock:
        _conn.execute("DELETE FROM media WHERE key = ?", (key,))
        _conn.commit()


# --- Статистика ---

def posted_since(cutoff_ts: float) -> list:
    """
    Опубликованные (status='posted') пачки не раньше cutoff_ts — источник
    для сбора статистики: 'results' содержит id постов по каждой площадке
    ({'threads': [...], 'instagram': [...], 'x': [...]}).

    chat_id/manifest/kit тоже отдаются — чтобы в отчёте было видно не только
    сколько всего опубликовано, но и какой конкретно пост, из какого
    источника (chat_id) и с каким заголовком.
    """
    with _lock:
        rows = _conn.execute(
            "SELECT id, chat_id, manifest, kit, results, updated_at FROM bursts"
            " WHERE status = 'posted' AND updated_at >= ?"
            " ORDER BY updated_at",
            (cutoff_ts,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            results = json.loads(r["results"] or "{}")
        except Exception:
            results = {}

        manifest = r["manifest"] or ""
        title = manifest.splitlines()[0][:120] if manifest else ""
        if not title and r["kit"]:
            try:
                title = (json.loads(r["kit"]).get("title") or "")[:120]
            except Exception:
                pass

        out.append({
            "id": r["id"],
            "chat_id": r["chat_id"],
            "title": title,
            "results": results,
            "updated_at": r["updated_at"],
        })
    return out


# --- Состояние ---

def get_state(key: str, default=None):
    with _lock:
        row = _conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str):
    with _lock:
        _conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        _conn.commit()
