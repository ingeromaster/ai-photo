#!/usr/bin/env python3
"""Telegram bot for Nano Banana Pro (kie.ai)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable

import requests
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from dotenv import load_dotenv

from db import ensure_user, list_telegram_ids, remaining_quota, try_consume_generation
from kie_client import (
    GENERATED_DIR,
    MAX_REFERENCE_IMAGES,
    create_task,
    extract_urls,
    guess_extension,
    safe_stem,
    save_reference_bytes,
    wait_for_result,
)
from logging_setup import setup_logging
from uploads_server import start_uploads_server

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
# Telegram sendPhoto limit is 10 MB; sendDocument allows up to 50 MB.
TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024
BROADCAST_DELAY_SEC = 0.05

log = setup_logging("tg-bot", "bot.log")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

_gen_lock = asyncio.Lock()
_user_photo_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_album_lock = asyncio.Lock()
_albums: dict[str, dict] = {}
ALBUM_WAIT_SEC = 1.5


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


class UpdateLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = data.get("event_update")
        update_id = getattr(update, "update_id", "?")
        user = data.get("event_from_user")
        user_label = f"user={user.id}" if user else "user=?"
        event_name = event.__class__.__name__
        log.info("update=%s %s event=%s", update_id, user_label, event_name)
        try:
            return await handler(event, data)
        except Exception:
            log.exception("update=%s %s failed event=%s", update_id, user_label, event_name)
            raise


class GenFSM(StatesGroup):
    collecting = State()
    choose_resolution = State()
    choose_aspect = State()


RESOLUTIONS = [
    ("1K (~1024px)", "1K"),
    ("2K (~2048px)", "2K"),
    ("4K (~4096px)", "4K"),
]

ASPECTS = [
    ("1:1", "1:1"),
    ("2:3 портрет", "2:3"),
    ("3:2 альбом", "3:2"),
    ("9:16", "9:16"),
    ("16:9", "16:9"),
    ("4:5", "4:5"),
    ("3:4", "3:4"),
    ("auto", "auto"),
]


def resolution_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"res:{value}")]
        for label, value in RESOLUTIONS
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def aspect_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row: list[InlineKeyboardButton] = []
    for label, value in ASPECTS:
        row.append(InlineKeyboardButton(text=label, callback_data=f"asp:{value}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


HELP_TEXT = (
    "AI Photosessions — Nano Banana Pro\n\n"
    "Как пользоваться:\n"
    "1) Пришлите до 8 референс-фото (можно альбомом) — необязательно\n"
    "2) Отправьте текстовый промпт\n"
    "3) Выберите качество и формат кадра\n"
    "4) Получите результат\n\n"
    "Команды:\n"
    "/start — справка\n"
    "/new — начать заново\n"
    "/cancel — отменить текущую сессию\n\n"
    "Лимит: 10 генераций на аккаунт (можно увеличить у администратора)."
)


async def _sync_user(message: Message) -> dict:
    user = message.from_user
    return await asyncio.to_thread(
        ensure_user,
        user.id,
        username=user.username,
        first_name=user.first_name,
    )


def _generate_sync(prompt: str, image_urls: list[str], resolution: str, aspect_ratio: str) -> dict:
    started = time.time()
    log.info(
        "kie.create start refs=%s resolution=%s aspect=%s prompt_len=%s",
        len(image_urls),
        resolution,
        aspect_ratio,
        len(prompt),
    )
    task_id = create_task(
        prompt,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        output_format="png",
        image_urls=image_urls,
    )
    log.info("kie.create ok taskId=%s", task_id)
    data = wait_for_result(task_id)
    urls = extract_urls(data)
    url = urls[0]
    ext = guess_extension(url, "png")
    filename = f"{int(time.time())}_{safe_stem(prompt)}_{uuid.uuid4().hex[:8]}_1{ext}"
    dest = GENERATED_DIR / filename
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    dest.write_bytes(response.content)
    elapsed = round(time.time() - started, 1)
    log.info(
        "kie.done taskId=%s file=%s size=%s bytes elapsed=%ss",
        task_id,
        dest.name,
        dest.stat().st_size,
        elapsed,
    )
    return {"path": str(dest), "taskId": task_id, "reference_urls": image_urls}


async def _send_generated_result(
    message: Message,
    *,
    path: Path,
    resolution: str,
    aspect: str,
    user_id: int,
    task_id: str,
    left_count: int,
) -> None:
    file_bytes = path.read_bytes()
    file_size = len(file_bytes)
    size_mb = file_size / (1024 * 1024)
    caption = (
        f"Готово · {resolution} · {aspect}\n"
        f"taskId: {task_id}\n"
        f"Осталось генераций: {left_count}"
    )

    log.info(
        "telegram.send start user=%s taskId=%s file=%s size_mb=%.2f",
        user_id,
        task_id,
        path.name,
        size_mb,
    )

    if file_size <= TG_PHOTO_MAX_BYTES:
        try:
            await message.answer_photo(
                BufferedInputFile(file_bytes, filename=path.name),
                caption=caption,
            )
            log.info("telegram.send photo ok user=%s taskId=%s", user_id, task_id)
        except TelegramBadRequest as exc:
            log.warning(
                "telegram.send photo failed user=%s taskId=%s error=%s",
                user_id,
                task_id,
                exc,
            )
            await message.answer("Превью не отправилось, отправляю файл документом…")
    else:
        log.warning(
            "telegram.skip photo user=%s taskId=%s size_mb=%.2f limit_mb=10",
            user_id,
            task_id,
            size_mb,
        )
        await message.answer(
            f"Файл {size_mb:.1f} МБ — слишком большой для превью в Telegram (лимит 10 МБ).\n"
            "Отправляю документом для скачивания."
        )

    try:
        await message.answer_document(
            BufferedInputFile(file_bytes, filename=path.name),
            caption="Файл для скачивания",
        )
        log.info("telegram.send document ok user=%s taskId=%s", user_id, task_id)
    except TelegramBadRequest as exc:
        log.error(
            "telegram.send document failed user=%s taskId=%s error=%s",
            user_id,
            task_id,
            exc,
        )
        await message.answer(
            "Генерация завершилась, но Telegram не принял файл.\n"
            f"taskId: {task_id}\n"
            "Попробуйте качество 1K."
        )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(GenFSM.collecting)
    await state.update_data(photos=[], prompt=None, resolution=None, aspect_ratio=None)
    user = await _sync_user(message)
    await message.answer(
        f"{HELP_TEXT}\n"
        f"Осталось генераций: {user['left_count']}"
    )


@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(GenFSM.collecting)
    await state.update_data(photos=[], prompt=None, resolution=None, aspect_ratio=None)
    await message.answer("Сессия сброшена. Пришлите фото (до 8) и/или промпт.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда только для администратора.")
        return

    text = (message.text or "")
    # Support "/broadcast text" and "/broadcast@bot text"
    parts = text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if not payload:
        await message.answer(
            "Использование:\n"
            "/broadcast Текст сообщения для всех пользователей\n\n"
            "Пример:\n"
            "/broadcast Всем привет! Добавили формат 2K."
        )
        return

    user_ids = await asyncio.to_thread(list_telegram_ids)
    if not user_ids:
        await message.answer("В базе пока нет пользователей для рассылки.")
        return

    await message.answer(f"Рассылка запущена: {len(user_ids)} получателей…")
    log.info("broadcast.start admin=%s recipients=%s", message.from_user.id, len(user_ids))

    ok = 0
    failed = 0
    blocked = 0
    for telegram_id in user_ids:
        try:
            await bot.send_message(telegram_id, payload)
            ok += 1
        except TelegramBadRequest as exc:
            failed += 1
            err = str(exc).lower()
            if "blocked" in err or "deactivated" in err or "chat not found" in err:
                blocked += 1
            log.warning("broadcast.fail user=%s error=%s", telegram_id, exc)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.exception("broadcast.fail user=%s error=%s", telegram_id, exc)
        await asyncio.sleep(BROADCAST_DELAY_SEC)

    report = (
        "Рассылка завершена.\n"
        f"Успешно: {ok}\n"
        f"Ошибки: {failed}\n"
        f"Из них недоступны/блок: {blocked}"
    )
    log.info(
        "broadcast.done admin=%s ok=%s failed=%s blocked=%s",
        message.from_user.id,
        ok,
        failed,
        blocked,
    )
    await message.answer(report)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Нажмите /start или /new, чтобы начать снова.")


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Отменено. Нажмите /start или /new.")
    await callback.answer()


async def _download_telegram_file(file_id: str) -> tuple[bytes, str]:
    file = await bot.get_file(file_id)
    buffer = BytesIO()
    await bot.download_file(file.file_path, buffer)
    data = buffer.getvalue()
    name = Path(file.file_path or "photo.jpg").name
    return data, name


def _guess_mime(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(suffix, "image/jpeg")


def _save_one_reference(data: bytes, filename: str, mime: str) -> str:
    return save_reference_bytes(data, filename, mime)


def _photo_added_text(count: int) -> str:
    return (
        f"Фото {count}/{MAX_REFERENCE_IMAGES} добавлено.\n"
        "Можете прислать ещё или отправить промпт текстом."
    )


async def _append_photos_and_reply(
    message: Message,
    state: FSMContext,
    new_urls: list[str],
) -> None:
    if not new_urls:
        return

    user_id = message.from_user.id
    async with _user_photo_locks[user_id]:
        data = await state.get_data()
        photos: list[str] = list(data.get("photos") or [])
        free = MAX_REFERENCE_IMAGES - len(photos)
        if free <= 0:
            await message.answer(
                f"Уже максимум {MAX_REFERENCE_IMAGES} фото. Пришлите промпт текстом."
            )
            return
        truncated = len(new_urls) > free
        if truncated:
            new_urls = new_urls[:free]
        photos.extend(new_urls)
        await state.update_data(photos=photos)
        count = len(photos)

    log.info("refs.added user=%s count=%s added=%s", user_id, count, len(new_urls))
    text = _photo_added_text(count)
    if truncated:
        text += f"\nЧасть фото не принята: лимит {MAX_REFERENCE_IMAGES}."
    await message.answer(text)


async def _finalize_album(album_key: str) -> None:
    try:
        await asyncio.sleep(ALBUM_WAIT_SEC)
    except asyncio.CancelledError:
        return

    async with _album_lock:
        album = _albums.pop(album_key, None)
    if not album or not album.get("urls"):
        return

    await _append_photos_and_reply(album["message"], album["state"], list(album["urls"]))


async def _handle_incoming_image(
    message: Message,
    state: FSMContext,
    *,
    file_id: str,
    filename: str,
    mime: str,
) -> None:
    try:
        raw, downloaded_name = await _download_telegram_file(file_id)
        if not Path(filename).suffix:
            filename = downloaded_name or filename
        url = await asyncio.to_thread(_save_one_reference, raw, filename, mime)
    except Exception as exc:  # noqa: BLE001
        log.exception("refs.save_failed user=%s error=%s", message.from_user.id, exc)
        await message.answer(f"Не удалось сохранить фото: {exc}")
        return

    media_group_id = message.media_group_id
    if not media_group_id:
        await _append_photos_and_reply(message, state, [url])
        return

    album_key = f"{message.from_user.id}:{media_group_id}"
    async with _album_lock:
        album = _albums.get(album_key)
        if album is None:
            album = {"urls": [], "message": message, "state": state, "task": None}
            _albums[album_key] = album
        album["urls"].append(url)
        album["message"] = message
        album["state"] = state
        old_task = album.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        album["task"] = asyncio.create_task(_finalize_album(album_key))


@dp.message(GenFSM.collecting, F.photo)
async def on_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    await _handle_incoming_image(
        message,
        state,
        file_id=photo.file_id,
        filename=f"{photo.file_unique_id}.jpg",
        mime="image/jpeg",
    )


@dp.message(GenFSM.collecting, F.document)
async def on_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not doc:
        return
    mime = (doc.mime_type or "").lower()
    name = doc.file_name or "image.jpg"
    suffix = Path(name).suffix.lower()
    ok = mime.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
    if not ok:
        await message.answer("Нужен файл изображения: JPG/PNG/WEBP/HEIC.")
        return

    await _handle_incoming_image(
        message,
        state,
        file_id=doc.file_id,
        filename=name,
        mime=mime or _guess_mime(name),
    )


@dp.message(GenFSM.collecting, F.text)
async def on_prompt(message: Message, state: FSMContext) -> None:
    prompt = (message.text or "").strip()
    if not prompt or prompt.startswith("/"):
        return
    if len(prompt) > 10000:
        await message.answer("Промпт слишком длинный (макс. 10000 символов).")
        return

    user_id = message.from_user.id
    await _sync_user(message)
    left = await asyncio.to_thread(remaining_quota, user_id)
    if left <= 0:
        await message.answer(
            "Лимит генераций исчерпан.\n"
            "Напишите администратору, чтобы увеличить квоту."
        )
        return

    for _ in range(10):
        async with _album_lock:
            pending = any(k.startswith(f"{user_id}:") for k in _albums)
        if not pending:
            break
        await asyncio.sleep(0.4)

    data = await state.get_data()
    photos = data.get("photos") or []
    await state.update_data(prompt=prompt)
    await state.set_state(GenFSM.choose_resolution)
    log.info("prompt.accepted user=%s refs=%s prompt_len=%s left=%s", user_id, len(photos), len(prompt), left)
    await message.answer(
        f"Промпт принят. Референсов: {len(photos)}.\n"
        f"Осталось генераций: {left}\n"
        "Выберите качество:",
        reply_markup=resolution_keyboard(),
    )


@dp.callback_query(GenFSM.choose_resolution, F.data.startswith("res:"))
async def on_resolution(callback: CallbackQuery, state: FSMContext) -> None:
    resolution = callback.data.split(":", 1)[1]
    await state.update_data(resolution=resolution)
    await state.set_state(GenFSM.choose_aspect)
    await callback.message.answer(
        f"Качество: {resolution}\nВыберите формат кадра:",
        reply_markup=aspect_keyboard(),
    )
    await callback.answer()


@dp.callback_query(GenFSM.choose_aspect, F.data.startswith("asp:"))
async def on_aspect(callback: CallbackQuery, state: FSMContext) -> None:
    aspect = callback.data.split(":", 1)[1]
    data = await state.get_data()
    prompt = data.get("prompt")
    resolution = data.get("resolution") or "1K"
    photos = data.get("photos") or []

    if not prompt:
        await callback.message.answer("Промпт потерян. Нажмите /new и начните снова.")
        await state.clear()
        await callback.answer()
        return

    user_id = callback.from_user.id
    await asyncio.to_thread(
        ensure_user,
        user_id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
    )
    consumed = await asyncio.to_thread(try_consume_generation, user_id)
    if not consumed:
        await callback.message.answer(
            "Лимит генераций исчерпан.\n"
            "Напишите администратору, чтобы увеличить квоту."
        )
        await state.clear()
        await callback.answer()
        return

    await state.clear()
    await callback.message.answer(
        f"Генерация {resolution} {aspect}, референсов: {len(photos)}…\nОбычно 20–90 секунд."
    )
    await callback.answer()
    log.info(
        "generate.request user=%s resolution=%s aspect=%s refs=%s prompt_len=%s left_after=%s",
        user_id,
        resolution,
        aspect,
        len(photos),
        len(prompt),
        consumed["left_count"],
    )

    try:
        async with _gen_lock:
            result = await asyncio.to_thread(
                _generate_sync,
                prompt,
                photos,
                resolution,
                aspect,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("generate.failed user=%s error=%s", user_id, exc)
        await callback.message.answer(f"Ошибка генерации: {exc}")
        return

    await _send_generated_result(
        callback.message,
        path=Path(result["path"]),
        resolution=resolution,
        aspect=aspect,
        user_id=user_id,
        task_id=result["taskId"],
        left_count=consumed["left_count"],
    )


@dp.message(F.text)
async def fallback_text(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нажмите /start, затем пришлите фото и промпт.")


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in .env")
    uploads_port = int(os.getenv("UPLOADS_PORT", "8080"))
    start_uploads_server(port=uploads_port)
    dp.update.middleware(UpdateLoggingMiddleware())
    me = await bot.get_me()
    log.info("Bot started as @%s (%s)", me.username, me.id)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
