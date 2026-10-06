"""Конфигурация из переменных окружения."""

import json
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
# ID групп-источников, например -1001234567890
# Можно указать несколько через запятую: "-1001234567890,-1009876543210" —
# слушатель подпишется на все сразу, паблишеру источник не важен (он видит
# только манифест и медиа, не то, из какого чата они пришли).
SOURCE_CHAT_IDS = [int(x.strip()) for x in _req("SOURCE_CHAT_ID").split(",") if x.strip()]
# Старое имя оставлено ради обратной совместимости кода, который может его
# импортировать напрямую (например, ручные /admin-скрипты) — всегда первый
# из списка, только для этого случая.
SOURCE_CHAT_ID = SOURCE_CHAT_IDS[0]

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

# Никогда не публиковать пост, если в тексте есть любая из этих фраз —
# в отличие от POST_FILTER_PHRASE (обязан совпасть), эта исключает.
# По умолчанию отсекает служебные заглушки самого бота-предиктора
# ("... needs a human because: panel split 5-5 of 10 ..."), которые
# утекают в кит как обычный текст площадки, когда панель моделей
# разошлась и бот сам просит ручную проверку вместо публикации.
# "rehearsal"/"lynaix" — тестовые прогоны предиктора (REHEARSAL LYNAIX-524):
# такие посты не должны попадать в соцсети.
POST_EXCLUDE_PHRASE = _opt("POST_EXCLUDE_PHRASE", "needs a human|rehearsal|lynaix")

# --- Маршрутизация: какой формат источника на какие площадки публикуется ---
# Три формата: kit (zip-набор), manifest (манифест + фото), legacy (текст без
# манифеста). Один матч приходит всеми тремя, поэтому у каждого формата своя
# роль, а не "всё на все площадки".
#
# По умолчанию: kit -> Instagram и X; manifest -> все площадки; legacy -> никуда.
# Переопределяется JSON-ом ROUTING: ключ "default" — для всех чатов, остальные
# ключи — chat_id источника (перекрывают default для этого чата). Формат,
# которого в записи нет, берётся из следующего уровня.
#
# Пример:
#   ROUTING={
#     "-1003996941088": {"manifest": ["threads"]},
#     "-1003952139185": {"manifest": ["threads"]}
#   }
_PLATFORM_NAMES = {"threads", "instagram", "x"}
_FORMATS = ("kit", "manifest", "legacy")
ROUTING_DEFAULT = {
    "kit": ["instagram", "x"],
    "manifest": ["threads", "instagram", "x"],
    "legacy": [],
}


def _parse_routing(raw: str) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        out = {}
        for chat, formats in data.items():
            key = "default" if chat == "default" else str(int(chat))
            out[key] = {}
            for fmt, platforms in formats.items():
                fmt = str(fmt).lower()
                if fmt not in _FORMATS:
                    raise ValueError(f"неизвестный формат {fmt!r} (допустимо: {', '.join(_FORMATS)})")
                names = [str(p).lower() for p in platforms]
                bad = [p for p in names if p not in _PLATFORM_NAMES]
                if bad:
                    raise ValueError(f"неизвестная площадка {bad} (допустимо: threads, instagram, x)")
                out[key][fmt] = names
        return out
    except Exception as e:
        raise RuntimeError(f"ROUTING: не удалось разобрать JSON — {e}")


ROUTING = _parse_routing(_opt("ROUTING", ""))

# --- Защита от дублей ---
# Один матч приходит несколькими путями (zip-кит, манифест + фото, старый
# текст без манифеста). Перед публикацией пост "занимает" ключ
# (матч + тип поста + площадка); второй такой же пост пропускается.
DEDUP_ENABLED = _opt("DEDUP_ENABLED", "true").lower() == "true"
# Сколько часов занятый ключ считается актуальным. Дольше — это уже другой
# матч тех же команд.
DEDUP_WINDOW_HOURS = float(_opt("DEDUP_WINDOW_HOURS", "18"))
# Запасной приоритет источников (zip-кит > манифест > старый текст), если
# маршрутизация ROUTING не развела форматы по площадкам: менее приоритетные
# ждут, пока лучший вариант займёт ключ матча:
#  - старый текст без манифеста — перед публикацией куда угодно;
#  - манифест — перед публикацией в Instagram и X (в Threads идёт сразу).
# По умолчанию 0 (не ждать). Старый текст опережает кит минут на 13 — если
# понадобится, 1500.
DEDUP_LEGACY_GRACE_SECONDS = int(_opt("DEDUP_LEGACY_GRACE_SECONDS", "0"))
DEDUP_MANIFEST_GRACE_SECONDS = int(_opt("DEDUP_MANIFEST_GRACE_SECONDS", "0"))

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

# Видео-кит от AI Match Lab / cs-match-lab (поле "videos" в kit.json).
# Временный рубильник: пока false — видео из зип-китов не подхватываются
# вообще, как будто их там нет (картинки из того же кита публикуются как
# обычно). Включить обратно — поставить true.
VIDEO_KIT_ENABLED = _opt("VIDEO_KIT_ENABLED", "true").lower() == "true"

# Генератор видео (ai-match-lab, режим --serve). Если задан, каждый zip-кит бота
# (в архиве есть blank/blank.json) дополнительно пересылается ему POST'ом на
# {VIDEO_GENERATOR_URL}/kit — он делает видео и кладёт готовый кит в группу,
# которую мы читаем. Пусто — пересылка выключена. Токен тот же, что KIT_API_TOKEN
# у генератора. Внутри одного Railway-проекта удобнее приватный адрес вида
# http://ai-match-lab.railway.internal:8080 (без публичного домена).
VIDEO_GENERATOR_URL = _opt("VIDEO_GENERATOR_URL").rstrip("/")
VIDEO_GENERATOR_TOKEN = _opt("VIDEO_GENERATOR_TOKEN")

if VIDEO_GENERATOR_URL and not VIDEO_GENERATOR_TOKEN:
    raise RuntimeError("VIDEO_GENERATOR_URL задан, а VIDEO_GENERATOR_TOKEN — нет")

# X: публиковать пост, только если в нём есть хотя бы одна картинка.
# Постов "голым текстом" в X быть не должно.
X_REQUIRE_IMAGE = _opt("X_REQUIRE_IMAGE", "true").lower() == "true"

# То же самое правило для Threads.
THREADS_REQUIRE_IMAGE = _opt("THREADS_REQUIRE_IMAGE", "true").lower() == "true"
# Убирать хештеги, пришедшие из источника.
STRIP_HASHTAGS = _opt("STRIP_HASHTAGS", "false").lower() == "true"

# Свои хештеги, дописываются в конец поста — общий набор "по умолчанию",
# используется, если для конкретного источника (см. SOURCE_HASHTAGS ниже)
# нет отдельных тегов, а также как теги для площадок из этого источника,
# которые в SOURCE_HASHTAGS не переопределены.
# Формат любой: "#cs2 #esports" или "cs2, esports".
# Теги, уже есть в тексте, повторно не добавляются.
THREADS_HASHTAGS = _opt("THREADS_HASHTAGS", "")
X_HASHTAGS = _opt("X_HASHTAGS", "")

# Хештеги по вертикали (футбол/UFC/esports/...), а не один общий набор на
# все посты — иначе предикт по футболу уходит с "#cs2 #esports" и наоборот.
# Вертикаль определяется тем, из какого именно Telegram-чата (SOURCE_CHAT_ID)
# пришёл пост — это надёжнее, чем парсить тип из текста манифеста.
#
# Формат — JSON, ключ: chat_id источника (то же число, что в SOURCE_CHAT_ID),
# значение: теги по площадкам. Площадка, не указанная для источника, берёт
# теги из общей переменной (THREADS_HASHTAGS/X_HASHTAGS/INSTAGRAM_HASHTAGS).
#
# Пример:
#   SOURCE_HASHTAGS={
#     "-1004346691060": {"x": "#cs2 #esports #coinplay", "threads": "#cs2 #esports"},
#     "-1004233066920": {"x": "#football #soccer #coinplay", "threads": "#football"},
#     "-1004314031415": {"x": "#ufc #mma #coinplay", "threads": "#ufc"}
#   }
_SOURCE_HASHTAGS_RAW = _opt("SOURCE_HASHTAGS", "")
if _SOURCE_HASHTAGS_RAW:
    try:
        SOURCE_HASHTAGS = {
            str(int(chat_id)): {str(k).lower(): v for k, v in tags.items()}
            for chat_id, tags in json.loads(_SOURCE_HASHTAGS_RAW).items()
        }
    except Exception as e:
        raise RuntimeError(f"SOURCE_HASHTAGS: не удалось разобрать JSON — {e}")
else:
    SOURCE_HASHTAGS = {}

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

# --- Статистика постов (просмотры/лайки по площадкам) ---
# Бот, которым отчёт шлётся в Telegram-чат (не юзер-сессия Telethon —
# обычный Bot API токен от @BotFather, добавленный в целевой чат).
TELEGRAM_BOT_TOKEN = _opt("TELEGRAM_BOT_TOKEN")
# Чат/группа, куда слать еженедельный и ручной отчёт. Отрицательное число
# для группы — например -1004364911823. Узнать: добавить бота в чат,
# написать туда что угодно, дёрнуть
# https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates и посмотреть chat.id.
STATS_CHAT_ID = _opt("STATS_CHAT_ID")
# Автоматический еженедельный отчёт включён, только если заданы и токен, и чат.
STATS_ENABLED = (
    _opt("STATS_ENABLED", "true").lower() == "true"
    and bool(TELEGRAM_BOT_TOKEN and STATS_CHAT_ID)
)
STATS_INTERVAL_DAYS = int(_opt("STATS_INTERVAL_DAYS", "7"))
# Не повторять попытку отправки раньше чем через это время после последней
# (успешной или нет) — защита от долбёжки Instagram/Threads/X API по кругу,
# если Telegram недоступен (неверный STATS_CHAT_ID, бот не в чате и т.п.).
STATS_RETRY_COOLDOWN_SECONDS = int(_opt("STATS_RETRY_COOLDOWN_SECONDS", str(3600)))
