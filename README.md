# ai-photo

Сервис ИИ-фотосессий. Генерация через [kie.ai](https://kie.ai/) — модель **Nano Banana Pro**.

## Веб-демо

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # KIE_API_KEY, PUBLIC_BASE_URL
python app.py          # http://SERVER:8080
```

## Telegram-бот

```bash
# в .env: TELEGRAM_BOT_TOKEN, TELEGRAM_DAILY_LIMIT
python bot.py          # long polling
```

Сценарий: фото (до 8, необязательно) → промпт → качество → формат → результат.

Бот: [@aiphotosessions_bot](https://t.me/aiphotosessions_bot)

## CLI

```bash
python generate.py "портрет в студии, мягкий свет"
```

## Важно

- Файл `.env` с ключами **не коммитьте** (уже в `.gitignore`).
- Ключ kie.ai: https://kie.ai/api-key
- Документация API: https://docs.kie.ai/
