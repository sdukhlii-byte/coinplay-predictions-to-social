"""HTTP-приёмник zip-китов бота: грабер присылает архив -> очередь -> видео.

Грабер (coinplay-predictions-to-social) уже читает группы «approvals» юзер-
сессией Telethon и скачивает каждый zip-кит. Увидев в архиве blank/blank.json,
он пересылает архив сюда POST'ом. Генератор строит видео (kit_source.py ->
generate.run) и кладёт готовый кит в группу вертикали (TELEGRAM_CHAT_ID_<V>),
откуда грабер публикует его как обычно. Второй вход в Telegram не нужен.

  POST /kit?name=furia-vs-auroragaming-1791297000.zip
       Authorization: Bearer <KIT_API_TOKEN>      тело — сам zip (до 25 МБ)
    202 {"status":"queued"}      принят, видео делается в фоне
    200 {"status":"duplicate"}   этот match_id уже в очереди/сделан — ничего не делаем
    401 / 413 / 422              нет токена / слишком большой / битый или неподдержанный кит
  GET /health                    {"status":"ok","queue":N}

Видео делает один воркер строго по очереди: каждый матч — платный вызов
видео-модели, параллелить смысла нет, а очередь переживает всплеск постов.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import re
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import kit_source
import state
from config import env_int, env_str

log = logging.getLogger("kit_server")

MAX_BYTES = 25 * 1024 * 1024
_NAME_OK = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(name: str) -> str:
    n = _NAME_OK.sub("_", os.path.basename(name or "")) or "kit.zip"
    return n if n.lower().endswith(".zip") else n + ".zip"


class KitService:
    """Очередь + воркер. Отделено от HTTP-слоя, чтобы тестироваться без сети."""

    def __init__(self, args, run, max_attempts: int = 2, inbox: str | None = None):
        self.args, self.run, self.max_attempts = args, run, max_attempts
        self.inbox = inbox or tempfile.mkdtemp(prefix="kit-inbox-")
        os.makedirs(self.inbox, exist_ok=True)
        self.q: queue.Queue = queue.Queue()
        self.pending: set[str] = set()
        self.lock = threading.Lock()
        self.attempts: dict[str, int] = {}
        self.worker = threading.Thread(target=self._loop, name="kit-worker", daemon=True)

    def start(self) -> None:
        self.worker.start()

    def submit(self, data: bytes, name: str) -> tuple[str, dict]:
        """-> (статус, детали): queued | duplicate; KitError пробрасывается."""
        path = os.path.join(self.inbox, f"{os.urandom(4).hex()}-{_safe_name(name)}")
        with open(path, "wb") as f:
            f.write(data)
        try:
            match = kit_source.match_from_kit(path)
        except kit_source.KitError:
            os.remove(path)
            raise
        key = str(match["id"])
        with self.lock:
            if key in self.pending or state.already_posted({key}):
                os.remove(path)
                return "duplicate", {"id": key}
            self.pending.add(key)
        self.q.put((key, match, path))
        return "queued", {"id": key, "queue": self.q.qsize()}

    def _loop(self) -> None:
        while True:
            key, match, path = self.q.get()
            try:
                self._process(key, match)
            except Exception:  # воркер не должен умирать из-за одного кита
                log.exception("kit-worker: необработанный сбой по %s", key)
            finally:
                with self.lock:
                    self.pending.discard(key)
                try:
                    os.remove(path)
                except OSError:
                    pass
                self.q.task_done()

    def _process(self, key: str, match: dict) -> None:
        label = f'{match["home"]} vs {match["away"]}'
        while True:
            try:
                self.run(match, self.args)
                state.mark_posted(key, competition=match.get("competition", ""))
                log.info("kit-worker: %s — готово", label)
                return
            except ValueError as e:
                # дата в прошлом и т.п. — повтор ничего не изменит и ничего не стоит
                state.mark_posted(key, competition=match.get("competition", ""))
                log.error("kit-worker: %s — отклонён без повторов: %s", label, e)
                return
            except Exception as e:
                n = self.attempts[key] = self.attempts.get(key, 0) + 1
                log.exception("kit-worker: %s не собран (попытка %d/%d): %s",
                              label, n, self.max_attempts, e)
                if n >= self.max_attempts:
                    state.mark_posted(key, competition=match.get("competition", ""))
                    log.error("kit-worker: %s — больше не пытаюсь", label)
                    return


def make_handler(service: KitService, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aml-kit/1"

        def log_message(self, fmt, *a):  # штатный access-log в stderr не нужен
            log.debug("http: " + fmt, *a)

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                self._json(200, {"status": "ok", "queue": service.q.qsize()})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != "/kit":
                return self._json(404, {"error": "not found"})
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return self._json(401, {"error": "bad token"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if n <= 0:
                return self._json(400, {"error": "empty body"})
            if n > MAX_BYTES:
                return self._json(413, {"error": f"больше {MAX_BYTES} байт"})
            data = self.rfile.read(n)
            name = (parse_qs(url.query).get("name") or ["kit.zip"])[0]
            try:
                status, info = service.submit(data, name)
            except kit_source.KitError as e:
                log.warning("kit %s отклонён: %s", name, e)
                return self._json(422, {"error": str(e)})
            log.info("kit %s -> %s %s", name, status, info)
            self._json(202 if status == "queued" else 200, {"status": status, **info})

    return Handler


class _DualStackServer(ThreadingHTTPServer):
    """Слушает и IPv4, и IPv6: приватная сеть Railway (*.railway.internal) в части
    окружений работает только по IPv6, а публичный домен — по IPv4."""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def _make_server(port: int, handler):
    try:
        return _DualStackServer(("::", port), handler)
    except OSError as e:  # в контейнере нет IPv6 — обычный IPv4
        log.info("kit-server: IPv6 недоступен (%s) — слушаю только IPv4", e)
        return ThreadingHTTPServer(("0.0.0.0", port), handler)


def serve(args, run, port: int | None = None, token: str | None = None,
          block: bool = True):
    """Поднимает приёмник. block=False возвращает (server, service) — для тестов."""
    token = token if token is not None else env_str("KIT_API_TOKEN")
    if not token:
        raise RuntimeError("Не задан KIT_API_TOKEN — без него приёмник открыт всем. "
                           "Придумай длинную случайную строку и задай её и здесь, и в "
                           "грабере (VIDEO_GENERATOR_TOKEN).")
    port = port if port is not None else env_int("PORT", 8080, lo=1, hi=65535)
    inbox = env_str("KIT_INBOX_DIR", os.path.join(os.path.dirname(state._path()) or ".", "inbox"))
    service = KitService(args, run, max_attempts=env_int("WATCH_MAX_ATTEMPTS", 2, lo=1, hi=10),
                         inbox=inbox)
    service.start()
    server = _make_server(port, make_handler(service, token))
    log.info("kit-server: слушаю :%d (POST /kit, GET /health)", server.server_address[1])
    if not block:
        return server, service
    try:
        server.serve_forever()
    finally:
        server.server_close()
