# Telegram-группа → Threads, Instagram и X: автопостинг

Читает группу, отбирает нужные посты по фразе-маркеру, схлопывает варианты
одного материала в один пост и публикует в Threads, Instagram и/или X (Twitter).
Каждая площадка включается своим флагом (`*_ENABLED`) и работает независимо.

## Почему юзер-сессия, а не бот

**Бот не получает сообщения других ботов.** Это ограничение Telegram Bot API,
которое не обходится правами админа. Если посты в группу пишет чужой бот,
наш бот их не увидит вообще.

Поэтому источник читается обычным аккаунтом через Telethon. Аккаунт должен
состоять в группе — больше ничего не требуется, права админа не нужны.

## Как работает

```
Группа в Telegram
      │ Telethon (юзер-сессия, читает всё)
      ▼
фильтр по фразе ──► пачка вариантов в SQLite
                          │ ждём BURST_WAIT, пока придут остальные варианты
                          ▼
                   выбор одного варианта (longest/first/last)
                          │
                   Threads API ──► качает медиа с /media/{key}
```

### Формат источника

Материал приходит россыпью, манифест связывает всё вместе:

```
[картинки]  01-picks.png · 02-analysis.png       отдельными сообщениями
[манифест]  threads · scoreboard · Team Spirit vs Luminosity Gaming
            01-picks.png · https://t.me/c/4346691060/11
            02-analysis.png · https://t.me/c/4346691060/12
[текст]     🤖 10 AI models pick ... 🎮 Link in bio
```

**Манифест — разделитель постов.** Новый манифест закрывает предыдущий пост
и открывает следующий. Это важнее таймаута: источник публикует посты подряд,
и по одному лишь времени их не разделить.

Картинки берутся **по ссылкам из манифеста** — надёжнее, чем угадывать, какие
вложения к какому посту относятся. Если ссылки не сработают, используются
вложения, пришедшие в ту же пачку.

У манифеста есть тип (`question`, `scoreboard`) — по нему можно фильтровать
через `MANIFEST_TYPES`, если в Threads нужны не все виды постов.

Если текст пришёл без манифеста, пачка собирается по таймауту как раньше.

**Ожидание текста.** Источник присылает картинки и манифест, а текст иногда
приходит на минуту позже. Пачка с манифестом не закрывается как пустая, пока
не истечёт `TEXT_WAIT_SECONDS` — иначе опоздавший текст создал бы новую пачку
и ушёл в публикацию без картинок.

## Переменные окружения

### Обязательные
| Переменная | Что это |
|---|---|
| `TELEGRAM_API_ID` | с https://my.telegram.org |
| `TELEGRAM_API_HASH` | оттуда же |
| `TELEGRAM_STRING_SESSION` | из `gen_session.py`, запускается локально |
| `SOURCE_CHAT_ID` | ID группы, например `-1001234567890` |
| `THREADS_ACCESS_TOKEN` | токен из User Token Generator (если Threads включён) |
| `THREADS_USER_ID` | id аккаунта Threads (если Threads включён) |
| `PUBLIC_BASE_URL` | публичный домен Railway, без слэша в конце |

### Необязательные
| Переменная | По умолчанию | Что делает |
|---|---|---|
| `DATA_DIR` | `/data` | каталог Volume: база и скачанные медиа |
| `POST_FILTER_PHRASE` | пусто | публиковать только посты с этой фразой |
| `STRIP_FILTER_PHRASE` | `false` | вырезать фразу из текста перед публикацией |
| `STRIP_HASHTAGS` | `false` | убирать хештеги из текста |
| `BURST_WINDOW_SECONDS` | `180` | окно, в котором сообщения считаются одной пачкой |
| `BURST_WAIT_SECONDS` | `45` | пауза после последнего сообщения пачки |
| `TEXT_WAIT_SECONDS` | `600` | сколько ждать текст после манифеста |
| `SELECT_STRATEGY` | `longest` | какой вариант публиковать: `longest`/`first`/`last` |
| `MANIFEST_TYPES` | пусто | публиковать только эти типы манифеста, через запятую |
| `THREADS_ENABLED` | `true` | публиковать в Threads |
| `INSTAGRAM_ENABLED` | `false` | публиковать в Instagram |
| `INSTAGRAM_ACCESS_TOKEN` | — | long-lived токен IG (если Instagram включён) |
| `INSTAGRAM_USER_ID` | — | id IG Business-аккаунта (если Instagram включён) |
| `INSTAGRAM_HASHTAGS` | пусто | свои хештеги в конец подписи IG |
| `INSTAGRAM_SELECT_STRATEGY` | `longest` | какой вариант текста уходит в IG |
| `X_ENABLED` | `false` | публиковать в X (Twitter) |
| `X_API_KEY` / `X_API_SECRET` | — | Consumer Keys из X Developer Portal |
| `X_ACCESS_TOKEN` / `X_ACCESS_SECRET` | — | Access Token с правами Read and write |
| `X_SELECT_STRATEGY` | `shortest` | какой вариант текста уходит в X |
| `X_TEXT_LIMIT` | `280` | лимит символов X |
| `X_LONG_TEXT_MODE` | `fit` | если текст длиннее лимита: `fit`/`thread`/`skip` |
| `THREADS_HASHTAGS` | пусто | свои хештеги в конец поста Threads |
| `X_HASHTAGS` | пусто | свои хештеги в конец поста X |

## Длинный текст в X

Источник присылает один текст на пост, и он обычно длиннее 280 символов.
Разрезание в тред работает плохо: картинки уходят в первый твит, а призыв
«Link in bio» — во второй, который почти никто не открывает.

Поэтому по умолчанию (`X_LONG_TEXT_MODE=fit`) текст ужимается в один твит:
последний блок (призыв) сохраняется всегда, вырезаются блоки из середины,
начиная с конца. Место под хештеги резервируется заранее.

Другие режимы: `thread` — прежнее поведение с разбивкой на `(1/2)`, `(2/2)`;
`skip` — не публиковать в X то, что не влезает.

## Хештеги

Источник ставит хештеги только в длинный вариант, а в X по умолчанию уходит
короткий — поэтому теги можно дописывать своим набором на каждую площадку:

```
X_HASHTAGS=#cs2 #esports #Coinplay
THREADS_HASHTAGS=#Coinplay
```

Формат свободный: `#cs2 #esports` и `cs2, esports` дают одно и то же.

- Теги, уже присутствующие в тексте, повторно не добавляются
- Если текст заканчивается строкой тегов, новые дописываются в неё
- Учитывается лимит символов: влезет столько тегов, сколько поместится,
  пост не будет разрезан в тред из-за хвоста тегов

## Публикация в Instagram

### Требования (обойти нельзя — ограничения Meta)

- Аккаунт Instagram должен быть **Business или Creator**.
- Он должен быть **связан с Facebook-страницей**. Страница может быть пустой
  (ноль постов, ноль подписчиков) — она нужна только технически, чтобы на
  связку «страница ↔ IG» выпустить токен. Личный IG через API не постит.
- Instagram скачивает картинки **по URL сам** (как Threads), поэтому сервис
  должен быть публично доступен (`PUBLIC_BASE_URL`) — у нас уже так.
- Пост **обязан** содержать хотя бы одну картинку/видео: пост из одного
  текста IG API не принимает (в отличие от Threads/X). Наш `01-picks.png` +
  `02-analysis.png` уходят каруселью.

### Получение токена с нуля (один раз, ~15 минут)

1. **Facebook-страница.** Если нет — создать любую пустую:
   facebook.com → Pages → Create.
2. **Связать IG с этой страницей.** В приложении Instagram: Settings →
   Accounts Center → добавить/подтвердить, что IG-аккаунт и FB-страница в
   одном Accounts Center. Убедиться, что IG переключён в Business/Creator
   (Settings → Account type and tools → Switch to professional account).
3. **Meta App.** developers.facebook.com → My Apps → **Create App** →
   тип **Business**. В приложение добавить продукт **Instagram Graph API**
   (или «Instagram» → «Instagram API setup with Facebook Login»).
4. **Права.** В настройках приложения запросить разрешения:
   `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement`. Для своего же аккаунта их можно использовать в
   режиме разработки/через Graph API Explorer без полного App Review, если
   аккаунт добавлен как роль в приложении.
5. **Токен.** developers.facebook.com → Tools → **Graph API Explorer**:
   выбрать своё приложение, выбрать нужную FB-страницу, отметить права из
   п.4, «Generate Access Token». Получится short-lived токен.
6. **Long-lived токен.** Обменять short-lived на long-lived (~60 дней):
   ```bash
   curl -G "https://graph.facebook.com/v21.0/oauth/access_token" \
     --data-urlencode "grant_type=fb_exchange_token" \
     --data-urlencode "client_id=<APP_ID>" \
     --data-urlencode "client_secret=<APP_SECRET>" \
     --data-urlencode "fb_exchange_token=<SHORT_LIVED_TOKEN>"
   ```
   Результат → в `INSTAGRAM_ACCESS_TOKEN`. Сервис сам продлевает его, когда
   остаётся меньше 10 дней.
7. **IG User ID.** Узнать id Business-аккаунта:
   ```bash
   # id страницы
   curl -G "https://graph.facebook.com/v21.0/me/accounts" \
     --data-urlencode "access_token=<LONG_LIVED_TOKEN>"
   # instagram_business_account этой страницы
   curl -G "https://graph.facebook.com/v21.0/<PAGE_ID>" \
     --data-urlencode "fields=instagram_business_account" \
     --data-urlencode "access_token=<LONG_LIVED_TOKEN>"
   ```
   Значение `instagram_business_account.id` → в `INSTAGRAM_USER_ID`.
8. Выставить `INSTAGRAM_ENABLED=true`, задать `INSTAGRAM_ACCESS_TOKEN` и
   `INSTAGRAM_USER_ID`, передеплоить. В логах при старте появится
   `Instagram-аккаунт: @... (id ...)` — значит токен рабочий.

### Лимиты Instagram API

- ~25 публикаций в сутки на аккаунт (rolling 24 ч).
- Подпись до 2200 символов (длиннее — ужимается, тредов у IG нет).
- Карусель: 2-10 элементов.
- Изображения — JPEG (PNG обычно проходят, но при проблемах с публикацией
  первым делом проверить формат картинок генератора).

## Публикация в X (Twitter)

### Ключи

X Developer Portal → проект → **Keys and tokens**:
- Consumer Keys → `X_API_KEY`, `X_API_SECRET`
- Access Token and Secret → `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`

Важно: в настройках приложения (User authentication settings) должно стоять
**Read and write**. Если права были Read only, токены нужно перевыпустить
после изменения — старые останутся только на чтение.

### Тарификация

С февраля 2026 X перешёл на pay-per-use, бесплатного тарифа для новых
разработчиков нет — кредиты покупаются заранее в Developer Console.

Ориентиры: около **$0.015 за пост** и около **$0.20 за пост с URL** в тексте.
Разница в 13 раз, поэтому прямые ссылки в постах невыгодны — формулировка
вида «Link in bio» попадает в дешёвую категорию.

При 10 постах в день выходит примерно $4-5 в месяц.

### Отличия площадок

| | Threads | Instagram | X |
|---|---|---|---|
| Лимит текста | 500 | 2200 | 280 |
| Вложений | до 20 | 2-10 | до 4 |
| Пост без медиа | можно | **нельзя** | можно |
| Как получает медиа | качает по URL | качает по URL | файл напрямую |
| Стратегия текста | `longest` | `longest` | `shortest` |

Площадки публикуются независимо: если одна упала, остальные всё равно
отработают, а при повторной попытке успешная не будет продублирована
(состояние по каждой площадке хранится отдельно в `results`).
| `WORKER_INTERVAL_SECONDS` | `10` | интервал воркера |
| `MAX_ATTEMPTS` | `5` | попыток публикации до статуса `failed` |
| `ALLOW_EMPTY_TEXT` | `true` | публиковать посты без текста |

## Запуск

### 1. Получить строку сессии (локально, один раз)

```bash
pip install telethon
python gen_session.py
```

Спросит `api_id`, `api_hash`, телефон и код из Telegram. Выведет строку сессии
и список чатов с их id — оттуда берётся `SOURCE_CHAT_ID`.

Строка сессии = полный доступ к аккаунту. Хранить как пароль, в git не коммитить.

### 2. Деплой на Railway

1. Залить репозиторий, создать сервис из GitHub.
2. **Подключить Volume**, примонтировать в `/data`. Без него при редеплое
   теряется история, и уже опубликованные посты уедут в Threads повторно.
3. Прописать переменные окружения.
4. Включить публичный домен (Settings → Networking → Generate Domain),
   положить его в `PUBLIC_BASE_URL`.

Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Эндпоинты

| Путь | Назначение |
|---|---|
| `GET /media/{key}` | отдача медиа для Threads |
| `GET /health` | healthcheck |
| `GET /status` | очередь и последние 20 пачек |
| `GET /dialogs` | список чатов с id — если не знаешь `SOURCE_CHAT_ID` |

## Что публикуется

- Текст, фото, видео, GIF, изображения-документы
- Альбомы → карусель в Threads
- Длинный текст → связанный тред с нумерацией `(1/2)`, режется по границе предложения
- Архивы, стикеры, голосовые игнорируются

## Токен Threads

Токен из User Token Generator **уже long-lived (~60 дней)**. Обменивать его через
`grant_type=th_exchange_token` не нужно — этот грант работает только с short-lived
токенами из OAuth-флоу и вернёт `Session key invalid`.

Сервис сам продлевает токен, когда остаётся меньше 10 дней. Вручную:

```bash
curl -G "https://graph.threads.net/refresh_access_token" \
  --data-urlencode "grant_type=th_refresh_token" \
  --data-urlencode "access_token=<TOKEN>"
```

## Ограничения Threads API

- 500 символов на пост (длиннее — режется в тред)
- ~250 публикаций в сутки
- до 20 элементов в карусели
- медиа Threads скачивает по URL, поэтому сервис должен быть публично доступен
