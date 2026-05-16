# Сглыпа-подобный бот для одного Telegram-канала

Бот работает только с каналом `-1003009758716`, учится на словах из постов канала, хранит память в `data/brain.json`, иногда пишет рандомный бред, отвечает реплаем, делает мемы, отправляет опросы и ставит реакции 👍/❤/🤡.

Панель управления доступна только пользователю `7877092881` в личке с ботом через `/start`.

## Что умеет

- читает `channel_post` и `edited_channel_post` только из канала `-1003009758716`;
- записывает слова, простые цепочки слов, последние сообщения и админ-алиасы в JSON;
- генерирует сообщения на 3-13 слов с рандомными эмодзи;
- иногда отвечает реплаем на посты и может тегать рандомного админа, если бот смог получить usernames через `getChatAdministrators`;
- делает мемы из `meme*.jpg/png/jpeg` и `mem*.jpg/png/jpeg` в корне проекта или папке `memes/`;
- триггеры мемов: `сделай мем`, `сделай мемчик`, `делай мем`, `бля`;
- если триггер написан реплаем на пост, текст для мема берётся из того поста;
- создаёт рандомные опросы, иногда с картинкой из meme/mem-шаблонов;
- через личную панель владельца умеет отправить бред, бред с тегом, мем, опрос, произвольный текст, реакцию на последний пост, скормить фразу в память, включить/выключить хаос.

## Структура проекта

```text
sglypa_channel_bot/
├─ main.py
├─ requirements.txt
├─ .env.example
├─ .python-version
├─ Dockerfile
├─ docker-compose.yml
├─ data/
│  ├─ brain.json
│  ├─ state.json
│  └─ generated/
├─ memes/
│  └─ README.md
├─ fonts/
│  └─ README.md
└─ sglypa_bot/
   ├─ brain.py
   ├─ config.py
   ├─ memes.py
   ├─ state.py
   └─ telegram_api.py
```

## Быстрый запуск на Python 3.14

```bash
cd sglypa_channel_bot
cp .env.example .env
```

В `.env` вставь токен:

```env
BOT_TOKEN=123456:REAL_TOKEN_FROM_BOTFATHER
```

Создай окружение именно на Python 3.14:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

На Windows:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

## Настройка Telegram

1. Создай бота в BotFather и вставь токен в `.env`.
2. Добавь бота админом в канал `-1003009758716`.
3. Дай права на публикацию сообщений, публикацию медиа/опросов и реакции, если такие настройки есть в твоём канале.
4. Напиши боту в личку `/start` с аккаунта `7877092881`.

Бот запускается long polling и при старте вызывает `deleteWebhook`, чтобы старый webhook не мешал `getUpdates`.

## Мемы

Положи картинки в `memes/` или в корень проекта:

```text
memes/meme1.jpg
memes/meme_cat.png
memes/mem_template_02.jpg
```

Если картинок нет, бот всё равно сделает простой тёмный шаблон с текстом, но лучше добавить свои картинки.

## Шрифты и emoji

Для кириллицы бот ищет системные DejaVu/Noto/Arial-шрифты. Для лучшего результата на Linux:

```bash
sudo apt update
sudo apt install fonts-dejavu fonts-noto fonts-noto-color-emoji
```

Можно указать конкретный шрифт:

```env
FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
```

Emoji в мемах рисуются через `pilmoji`, а если он не смог загрузить emoji-ассет, бот откатывается к обычному Pillow-тексту. Сами сообщения в Telegram всегда могут содержать emoji.

## Docker

```bash
docker compose up --build -d
```

`docker-compose.yml` монтирует `data/` и `memes/`, чтобы память и картинки не пропадали при пересборке.

## Настройка частоты

В `.env` можно уменьшить или увеличить хаос:

```env
ON_MESSAGE_ACTION_CHANCE=0.04
REACTION_CHANCE=0.12
IDLE_MIN_SECONDS=240
IDLE_MAX_SECONDS=900
IDLE_ACTION_CHANCE=0.28
POLL_MEDIA_CHANCE=0.35
```

Чем меньше `*_CHANCE`, тем реже бот вмешивается. Значения `0.04` означает 4%.

## Команды владельца

- `/start` или `/panel` — панель управления;
- `/stats` — статистика памяти;
- `/cancel` — сброс режима ввода.

Все остальные пользователи в личке игнорируются.

## Важные нюансы каналов

В каналах Telegram часто не отдаёт реального пользователя-автора поста в `channel_post`, поэтому бот учит текст из постов канала, а админов для тегов пытается получить через `getChatAdministrators`. Если у админа нет username, бот не сможет сделать кликабельный `@tag` для него.

## Тагир через OpenAI

В этой версии добавлен режим собеседника. Если новый пост в канале начинается со слова `тагир`, бот отвечает в канал реплаем на этот пост.

Примеры:

```text
тагир как дела?
Тагир, какая погода в Лондоне?
tagir объясни что происходит
```

Нужно добавить переменные окружения на хостинге:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5-mini
OPENAI_WEB_SEARCH=true
TAGIR_ENABLED=true
TAGIR_NAME=тагир
```

Ключ OpenAI не надо класть в `.env` на GitHub, особенно если репозиторий публичный. Добавляй его только в переменные окружения Bothost.

`OPENAI_WEB_SEARCH=true` нужен для свежих вопросов вроде погоды, новостей и текущих фактов. Если аккаунт или выбранная модель не поддерживают веб-поиск, бот залогирует ошибку и автоматически попробует ответить без веб-поиска.

Для отключения Тагира поставь:

```env
TAGIR_ENABLED=false
```
