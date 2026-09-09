"""Конфигурация из переменных окружения."""

import os


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return v


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --- Telegram (юзер-сессия, не бот) ---
# Бот не видит сообщения других ботов, поэтому читаем группу обычным аккаунтом.
# api_id / api_hash берутся на https://my.telegram.org -> API development tools
TELEGRAM_API_ID = int(_req("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = _req("TELEGRAM_API_HASH")
# Строка сессии, получается один раз локально через gen_session.py
TELEGRAM_STRING_SESSION = _req("TELEGRAM_STRING_SESSION")
# ID группы-источника, например -1001234567890
SOURCE_CHAT_ID = int(_req("SOURCE_CHAT_ID"))

# --- Threads ---
THREADS_ENABLED = _opt("THREADS_ENABLED", "true").lower() == "true"
THREADS_ACCESS_TOKEN = _opt("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = _opt("THREADS_USER_ID")

if THREADS_ENABLED and not (THREADS_ACCESS_TOKEN and THREADS_USER_ID):
    raise RuntimeError(
        "THREADS_ENABLED=true, но не заданы THREADS_ACCESS_TOKEN / THREADS_USER_ID"
    )

# --- Instagram ---
# Публикация возможна только с Business/Creator аккаунта, связанного с
# Facebook-страницей. Токен и user_id берутся из Meta App с правом
# instagram_content_publish (см. README).
INSTAGRAM_ENABLED = _opt("INSTAGRAM_ENABLED", "false").lower() == "true"
INSTAGRAM_ACCESS_TOKEN = _opt("INSTAGRAM_ACCESS_TOKEN")
INSTAGRAM_USER_ID = _opt("INSTAGRAM_USER_ID")
INSTAGRAM_HASHTAGS = _opt("INSTAGRAM_HASHTAGS", "")
# Какой вариант текста уходит в IG. Лимит подписи большой (2200), поэтому
# по умолчанию берётся длинный вариант, как у Threads.
INSTAGRAM_SELECT_STRATEGY = _opt("INSTAGRAM_SELECT_STRATEGY", "longest").lower()

if INSTAGRAM_ENABLED and not (INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID):
    raise RuntimeError(
        "INSTAGRAM_ENABLED=true, но не заданы "
        "INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID"
    )

# --- X (Twitter) ---
# OAuth 1.0a user context: ключи приложения + токены доступа своего аккаунта.
# Берутся в X Developer Portal -> Keys and tokens.
X_ENABLED = _opt("X_ENABLED", "false").lower() == "true"
X_API_KEY = _opt("X_API_KEY")
X_API_SECRET = _opt("X_API_SECRET")
X_ACCESS_TOKEN = _opt("X_ACCESS_TOKEN")
X_ACCESS_SECRET = _opt("X_ACCESS_SECRET")
X_TEXT_LIMIT = int(_opt("X_TEXT_LIMIT", "280"))
# Что делать, если текст не влезает в лимит X:
#   fit    — ужать в один твит, сохранив призыв в конце (по умолчанию)
#   thread — разрезать на связанные твиты (1/2), (2/2)
#   skip   — не публиковать в X
X_LONG_TEXT_MODE = _opt("X_LONG_TEXT_MODE", "fit").lower()

if X_ENABLED and not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET]):
    raise RuntimeError(
        "X_ENABLED=true, но заданы не все ключи: "
        "X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET"
    )

# --- Сервис ---
# Публичный адрес на Railway без слэша в конце - по нему Threads качает медиа.
PUBLIC_BASE_URL = _req("PUBLIC_BASE_URL").rstrip("/")

# Секрет для ручных /admin-эндпоинтов (например, повторной публикации
# старого поста на новую площадку). Пусто = эндпоинты отключены.
ADMIN_TOKEN = _opt("ADMIN_TOKEN", "")

# Каталог Railway Volume. Тут лежат и база, и скачанные медиафайлы.
DATA_DIR = _opt("DATA_DIR", "/data")
DB_PATH = _opt("DB_PATH", os.path.join(DATA_DIR, "app.db"))
MEDIA_DIR = _opt("MEDIA_DIR", os.path.join(DATA_DIR, "media"))

# --- Фильтр постов ---
# Публиковать только посты с этой фразой. Пусто = все.
# Несколько вариантов через | (сработает любой).
POST_FILTER_PHRASE = _opt("POST_FILTER_PHRASE", "")
# Вырезать фразу-маркер из текста перед публикацией.
STRIP_FILTER_PHRASE = _opt("STRIP_FILTER_PHRASE", "false").lower() == "true"

# --- Схлопывание дублей ---
# Один матч приходит несколькими вариантами подряд (instagram, x, длинный).
# Сообщения, пришедшие в пределах этого окна, считаются одной пачкой.
BURST_WINDOW_SECONDS = int(_opt("BURST_WINDOW_SECONDS", "180"))
# Сколько ждать после последнего сообщения пачки, прежде чем публиковать.
BURST_WAIT_SECONDS = int(_opt("BURST_WAIT_SECONDS", "45"))
# Сколько ждать текст, если манифест и картинки уже пришли.
# Пачка с манифестом не закрывается как пустая, пока не истечёт это время:
# текст от источника иногда приходит на минуту позже картинок.
TEXT_WAIT_SECONDS = int(_opt("TEXT_WAIT_SECONDS", "600"))

# Какой вариант из пачки публиковать: longest | shortest | first | last
# Для Threads (лимит 500) обычно подходит длинный вариант.
SELECT_STRATEGY = _opt("SELECT_STRATEGY", "longest").lower()

# Отдельная стратегия для X: лимит 280 символов, поэтому по умолчанию
# берётся самый короткий вариант, чтобы не резать пост в тред.
X_SELECT_STRATEGY = _opt("X_SELECT_STRATEGY", "shortest").lower()

# Публиковать только манифесты этих типов, через запятую.
# Тип берётся из шапки: "threads · scoreboard · Матч" -> scoreboard.
# Пусто = публиковать все типы.
MANIFEST_TYPES = _opt("MANIFEST_TYPES", "")

# --- Прочее ---
WORKER_INTERVAL_SECONDS = int(_opt("WORKER_INTERVAL_SECONDS", "10"))
MAX_ATTEMPTS = int(_opt("MAX_ATTEMPTS", "5"))
ALLOW_EMPTY_TEXT = _opt("ALLOW_EMPTY_TEXT", "true").lower() == "true"
# Убирать хештеги, пришедшие из источника.
STRIP_HASHTAGS = _opt("STRIP_HASHTAGS", "false").lower() == "true"

# Свои хештеги, дописываются в конец поста.
# Формат любой: "#cs2 #esports" или "cs2, esports".
# Теги, уже есть в тексте, повторно не добавляются.
THREADS_HASHTAGS = _opt("THREADS_HASHTAGS", "")
X_HASHTAGS = _opt("X_HASHTAGS", "")

# --- Константы Threads API ---
THREADS_TEXT_LIMIT = 500
THREADS_GRAPH = "https://graph.threads.net/v1.0"

# --- Константы Instagram API ---
INSTAGRAM_CAPTION_LIMIT = 2200
# Токен получен через Facebook Login (FB-страница + Graph API Explorer,
# токен вида "EAA...") -> используем домен graph.facebook.com.
# (Есть отдельный флоу "Instagram API с прямым Instagram Login" с токенами
# вида "IGAA...", который ходит через graph.instagram.com — это НЕ наш
# случай, не перепутать при повторной настройке в будущем.)
IG_GRAPH = "https://graph.facebook.com/v21.0"

# Для автопродления токена (fb_exchange_token) нужны данные приложения —
# те же, что в Meta App: developers.facebook.com/apps/<APP_ID>/settings/basic/
META_APP_ID = _opt("META_APP_ID")
META_APP_SECRET = _opt("META_APP_SECRET")
