# ai-photo

Сервис ИИ-фотосессий. Генерация через [kie.ai](https://kie.ai/) — модель **Nano Banana Pro**.

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # затем впишите KIE_API_KEY
python generate.py "портрет в студии, мягкий свет"
```

Результат сохранится в `outputs/`.

### Опции

```bash
python generate.py "ваш промпт" --aspect-ratio 9:16 --resolution 1K --format png
python generate.py "edit this" --image https://example.com/photo.jpg
python generate.py "test" --no-download   # только URL, без скачивания
```

## Важно

- Файл `.env` с ключом **не коммитьте** (уже в `.gitignore`).
- Ключ: https://kie.ai/api-key
- Документация API: https://docs.kie.ai/
