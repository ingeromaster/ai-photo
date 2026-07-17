# ai-photo

Telegram-бот ИИ-фотосессий. Генерация через [kie.ai](https://kie.ai/) — **Nano Banana Pro**.

Бот: [@aiphotosessions_bot](https://t.me/aiphotosessions_bot)

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполните ключи
python bot.py
```

Сценарий: фото (до 8, необязательно) → промпт → качество → формат → результат.

Лимит: **10 генераций** на пользователя (PostgreSQL). Примеры SQL: `sql_examples.sql`.

Рассылка (админ): `/broadcast Текст сообщения`

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `KIE_API_KEY` | ключ kie.ai |
| `PUBLIC_BASE_URL` | публичный URL сервера (для референсов) |
| `UPLOADS_PORT` | порт раздачи `/uploads` (по умолчанию 8080) |
| `TELEGRAM_BOT_TOKEN` | токен бота |
| `TELEGRAM_ADMIN_IDS` | Telegram ID админов через запятую |
| `DATABASE_URL` | PostgreSQL |
| `DEFAULT_MAX_GENERATIONS` | лимит новым пользователям (10) |

## PostgreSQL

- Host: `127.0.0.1:5432` (через SSH-туннель с ноутбука)
- DB / user: `ai_photo`
- Schema: `schema.sql`

```bash
ssh -L 5432:127.0.0.1:5432 root@YOUR_SERVER_IP
```

## Важно

- `.env` не коммитить
- Документация kie.ai: https://docs.kie.ai/
