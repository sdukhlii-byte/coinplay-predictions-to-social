"""Одноразово получить refresh-токен YouTube для переменной YOUTUBE_REFRESH_TOKEN.

Запуск на своём компьютере (нужен браузер):
    python3 youtube_auth.py client_secret_XXXX.json

Откроется окно входа Google: войди тем аккаунтом, которому принадлежит канал,
и разреши доступ. Предупреждение «приложение не проверено» — нормально для
личного проекта: Advanced -> Go to ... (unsafe). Скрипт выведет токен — его
вставляют в Railway, в чат его слать не нужно.
"""

import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import requests

SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        raw = json.load(f)
    cfg = raw.get("installed") or raw.get("web")
    if not cfg:
        print("Нужен JSON клиента типа Desktop app")
        return 2

    state = secrets.token_urlsafe(16)
    result = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q or "error" in q:
                result.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("Готово, можно закрыть вкладку и вернуться в терминал.".encode())
                done.set()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = cfg["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"], "redirect_uri": redirect,
        "response_type": "code", "scope": SCOPE, "state": state,
        "access_type": "offline", "prompt": "consent",
    })
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("Открываю браузер. Если не открылся, перейди по ссылке:\n" + url)
    webbrowser.open(url)
    if not done.wait(300):
        print("Не дождался входа за 5 минут")
        return 1
    server.shutdown()

    if result.get("state") != state or "code" not in result:
        print("Вход не удался:", result.get("error", "неверный state"))
        return 1
    r = requests.post(cfg["token_uri"], data={
        "code": result["code"], "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"], "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }, timeout=30)
    data = r.json()
    if "refresh_token" not in data:
        print("Google не вернул refresh_token:", data)
        return 1
    print("\nYOUTUBE_REFRESH_TOKEN=" + data["refresh_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
