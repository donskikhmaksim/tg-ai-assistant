# tg-ai-assistant

Telegram AI-ассистент: читает личную (Telegram Business) и групповую переписку,
батчами прогоняет её через локальную LLM (Qwen-триаж) и Claude (разбор),
извлекает задачи / договорённости / обещания и создаёт их в TickTick через
существующий Railway-сервер `ticktick-mcp`.

Обработка **не realtime**, пакетная. Дешёвый локальный триаж отсеивает мусор,
чтобы не жечь токены Claude.

## Как это работает

```
Telegram
  ├─ business_message  (личка: вход + исходящие владельца)  ─┐
  └─ message           (группы, privacy off)                ─┤→ один бот / один бэк
                                                              ↓ КАЖДЫЙ апдейт сразу в БД
                                          Mongo: raw_messages (TTL 30д, ключ chatId)
                                                              ↓ APScheduler, каждые 30 мин
                       для каждого «грязного» чата → текущее ОКНО РАЗГОВОРА
                                                              ↓
                       Tier 1 — Qwen (Ollama): в окне есть задача? да/нет
                                                              ↓ только «да»
                       Tier 2 — Claude (opus-4-8): окно + долговременная память чата
                                                  → инкрементальный JSON
                                                              ↓ дедуп (sha1)
                       резолв проекта: chat_project_map[chatId] ?? Inbox
                                                              ↓
                       TickTick через Railway-MCP (create_task / complete_task)
```

**Два независимых механизма памяти** (не путать):
- **Окно разговора** (gap 6 ч, потолок 48 ч) — какие свежие сырые сообщения
  смотреть сейчас.
- **Долговременная память** (`chat_summary` + открытые задачи) — предыстория,
  не зависит от TTL сырья. Claude на каждом прогоне обновляет резюме, поэтому
  тема, всплывшая через неделю, разбирается с полным контекстом.

## Структура

```
app/
  __main__.py          точка входа: бот (polling) + планировщик
  config.py            конфиг из ENV
  models.py            доменные модели + JSON-схемы структурного вывода
  dedup.py             нормализация задачи + sha1 dedup-хеш
  db/
    mongo.py           Motor-клиент, индексы (TTL, уникальный dedupHash)
    repositories.py    CRUD по коллекциям
  bot/
    factory.py         сборка Bot/Dispatcher
    ingest.py          апдейт → raw_messages, определение direction
    handlers.py        business_connection/message, группы, меню привязки
    keyboards.py       reply-меню + инлайн-кнопки
  llm/
    qwen.py            Tier 1 — триаж (Ollama, OpenAI-совместимый API)
    claude.py          Tier 2 — разбор (claude-opus-4-8, structured output, кэш)
  mcp/
    ticktick.py        MCP-клиент к ticktick-mcp (SSE/Streamable HTTP)
  pipeline/
    windows.py         сборка окна разговора
    processor.py       оркестрация чата и прогона
    scheduler.py       APScheduler
tests/                 юнит-тесты (окна, дедуп)
```

## Запуск

### 1. Telegram (@BotFather)
- `/newbot` → `BOT_TOKEN`
- Bot Settings → **Business Mode → Enable**
- Bot Settings → **Group Privacy → Turn off**
- Подключить личку: Settings → Telegram Business → Chatbots → юзернейм бота →
  выдать права → область чатов «все».
- Бота добавить в нужные группы участником.

### 2. Конфигурация
```bash
cp .env.example .env   # заполнить токены и URL
```

### 3. Локально
```bash
pip install -r requirements.txt
python -m app
```

### 4. Railway
Проект собирается из `Dockerfile` (см. `railway.json`). Разверните рядом с
`ticktick-mcp`, задайте переменные окружения из `.env.example`. Qwen/Ollama —
локально на Mac mini, в `QWEN_BASE_URL` укажите его адрес.

### Тесты
```bash
pip install pytest
pytest
```

## Бот: управление (Фаза 1)
- `/start` в личке с ботом → приветствие + нижнее меню:
  `🔗 Привязать проект`, `📋 Мои привязки`, `❌ Отвязать`.
- **Привязка лички:** «🔗 Привязать проект» → перешлите сообщение из нужного
  чата (или пришлите `user_<id>` / `group_<id>`) → выберите проект кнопкой.
- **Привязка группы:** команда `/bind` прямо в группе → выбор проекта.
- Без привязки задачи уходят в проект `DEFAULT_PROJECT` (Inbox).

Уведомлений о созданных задачах нет — они просто появляются в TickTick.

## Конфигурация (ENV)
См. `.env.example`. Ключевые дефолты: `ANTHROPIC_MODEL=claude-opus-4-8`,
`QWEN_MODEL=qwen2.5:32b-instruct`, `BATCH_INTERVAL_MIN=30`, `CONV_GAP_HOURS=6`,
`MAX_LOOKBACK_HOURS=48`, `RAW_TTL_DAYS=30`, `DEFAULT_PROJECT=Inbox`.

## Заметки по реализации
- Имена тулов `ticktick-mcp` подтверждены по схеме сервера: `get_projects`,
  `create_task`, `update_task`, `complete_task`. Транспорт по умолчанию — SSE
  (`TICKTICK_MCP_TRANSPORT=sse`); переключается на `streamable-http`.
- Claude не ходит в MCP — возвращает только JSON; создание задач делает бэкенд.
- Идемпотентность: уникальный индекс на `dedupHash` + инкрементальный промпт.
- Сырьё атомарно пишется в БД до любой обработки; потеря безвозвратна.
