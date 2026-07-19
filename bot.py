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
from urllib.parse import urlparse

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
    InputMediaPhoto,
    Message,
    TelegramObject,
)
from dotenv import load_dotenv

from db import (
    MAX_REFERENCE_PACKS,
    count_packs,
    create_pack,
    delete_pack,
    ensure_user,
    get_images_for_packs,
    list_packs,
    list_telegram_ids,
    next_pack_title,
    remaining_quota,
    rename_pack,
    try_consume_generation,
)
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
    idle = State()
    adding_refs = State()
    naming_pack = State()
    renaming_pack = State()
    choosing_packs = State()
    awaiting_prompt = State()
    choose_resolution = State()
    choose_aspect = State()


MAX_PACK_TITLE_LEN = 64


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


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Новая генерация", callback_data="menu:generate")],
            [InlineKeyboardButton(text="📁 Мои референсы", callback_data="menu:refs")],
            [InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help")],
        ]
    )


def generate_refs_keyboard(*, has_packs: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Без референсов", callback_data="gen:none")],
    ]
    if has_packs:
        rows.append(
            [InlineKeyboardButton(text="Выбрать сохранённый набор", callback_data="gen:pick")]
        )
    rows.append(
        [InlineKeyboardButton(text="Загрузить новый набор", callback_data="gen:upload")]
    )
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def packs_select_keyboard(packs: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for pack in packs:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{pack['title']} ({pack['image_count']})",
                    callback_data=f"pack:{pack['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="gen:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def packs_manage_keyboard(packs: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for pack in packs:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👁 {pack['title']} ({pack['image_count']})",
                    callback_data=f"refs:view:{pack['id']}",
                ),
                InlineKeyboardButton(
                    text="✏️",
                    callback_data=f"refs:rename:{pack['id']}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"refs:del:{pack['id']}",
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить набор", callback_data="refs:add")]
    )
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def save_refs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💾 Сохранить набор", callback_data="refs:save")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]
    )


def name_pack_keyboard(*, auto_title: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Авто: {auto_title}",
                    callback_data="refs:autoname",
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]
    )


def normalize_pack_title(raw: str) -> str | None:
    title = " ".join((raw or "").split())
    if not title:
        return None
    if len(title) > MAX_PACK_TITLE_LEN:
        title = title[:MAX_PACK_TITLE_LEN].rstrip()
    return title


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
    "1) Сохраните до 5 референс-наборов (в каждом до 8 фото) и дайте им названия\n"
    "2) При генерации выберите один сохранённый набор\n"
    "3) Бот покажет выбранные фото снова — проверьте их\n"
    "4) Отправьте промпт → качество → формат\n\n"
    "Команды:\n"
    "/start — меню\n"
    "/new — в меню (наборы не удаляются)\n"
    "/refs — мои референсы\n"
    "/cancel — отменить текущий шаг\n\n"
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


async def _go_idle(message: Message, state: FSMContext, *, text: str | None = None) -> None:
    await state.clear()
    await state.set_state(GenFSM.idle)
    await state.update_data(
        pending_refs=[],
        selected_pack_ids=[],
        active_refs=[],
        prompt=None,
        resolution=None,
        aspect_ratio=None,
        after_save_use=False,
    )
    body = text or "Выберите действие:"
    await message.answer(body, reply_markup=main_menu_keyboard())


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


def _local_path_from_public_url(public_url: str) -> str | None:
    path = urlparse(public_url).path
    if "/uploads/" not in path:
        return None
    name = path.rsplit("/", 1)[-1]
    return name or None


async def _resend_reference_photos(
    message: Message,
    refs: list[dict[str, Any]],
    *,
    caption: str,
) -> None:
    if not refs:
        return
    media: list[InputMediaPhoto] = []
    for idx, item in enumerate(refs):
        media.append(
            InputMediaPhoto(
                media=item["telegram_file_id"],
                caption=caption if idx == 0 else None,
            )
        )
    try:
        await message.answer_media_group(media)
    except TelegramBadRequest as exc:
        log.warning("refs.resend_failed error=%s — fallback to urls count=%s", exc, len(refs))
        await message.answer(f"{caption}\n(не удалось повторно показать фото: {exc})")


async def _begin_prompt_with_refs(
    message: Message,
    state: FSMContext,
    refs: list[dict[str, Any]],
    *,
    intro: str,
) -> None:
    await state.update_data(active_refs=refs, prompt=None)
    await state.set_state(GenFSM.awaiting_prompt)
    if refs:
        await _resend_reference_photos(
            message,
            refs,
            caption="Референсы для этой генерации:",
        )
        await message.answer(
            f"{intro}\n"
            f"Референсов: {len(refs)}.\n"
            "Теперь отправьте текстовый промпт."
        )
    else:
        await message.answer(
            f"{intro}\n"
            "Референсы не выбраны.\n"
            "Отправьте текстовый промпт."
        )


async def _show_generate_start(message: Message, state: FSMContext, user_id: int) -> None:
    packs = await asyncio.to_thread(list_packs, user_id)
    await state.set_state(GenFSM.idle)
    await state.update_data(selected_pack_ids=[], active_refs=[], pending_refs=[])
    await message.answer(
        "Новая генерация.\nКак использовать референсы?",
        reply_markup=generate_refs_keyboard(has_packs=bool(packs)),
    )


async def _show_refs_manager(message: Message, user_id: int) -> None:
    packs = await asyncio.to_thread(list_packs, user_id)
    if not packs:
        await message.answer(
            "Сохранённых референсов пока нет.\n"
            "Добавьте набор фото — он сохранится и будет доступен для следующих генераций.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить набор", callback_data="refs:add")],
                    [InlineKeyboardButton(text="« В меню", callback_data="menu:home")],
                ]
            ),
        )
        return
    await message.answer(
        f"Ваши референс-наборы ({len(packs)}/{MAX_REFERENCE_PACKS}):\n"
        "👁 — посмотреть, ✏️ — переименовать, 🗑 — удалить.",
        reply_markup=packs_manage_keyboard(packs),
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user = await _sync_user(message)
    await _go_idle(
        message,
        state,
        text=(
            f"{HELP_TEXT}\n"
            f"Осталось генераций: {user['left_count']}"
        ),
    )


@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    await _sync_user(message)
    await _go_idle(
        message,
        state,
        text="Сессия сброшена. Сохранённые референсы на месте. Выберите действие:",
    )


@dp.message(Command("refs"))
async def cmd_refs(message: Message, state: FSMContext) -> None:
    await _sync_user(message)
    current = await state.get_state()
    if current not in {GenFSM.idle.state, None}:
        # Allow browsing packs without killing an in-progress generate unless adding.
        pass
    await state.set_state(GenFSM.idle)
    await _show_refs_manager(message, message.from_user.id)


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда только для администратора.")
        return

    text = (message.text or "")
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
    await _go_idle(message, state, text="Отменено. Выберите действие:")


@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await _go_idle(callback.message, state, text="Отменено. Выберите действие:")
    await callback.answer()


@dp.callback_query(F.data == "menu:home")
async def cb_menu_home(callback: CallbackQuery, state: FSMContext) -> None:
    await _go_idle(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(callback: CallbackQuery, state: FSMContext) -> None:
    user = await asyncio.to_thread(
        ensure_user,
        callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
    )
    await callback.message.answer(
        f"{HELP_TEXT}\nОсталось генераций: {user['left_count']}",
        reply_markup=main_menu_keyboard(),
    )
    await state.set_state(GenFSM.idle)
    await callback.answer()


@dp.callback_query(F.data == "menu:generate")
@dp.callback_query(F.data == "gen:start")
async def cb_menu_generate(callback: CallbackQuery, state: FSMContext) -> None:
    await asyncio.to_thread(
        ensure_user,
        callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
    )
    await _show_generate_start(callback.message, state, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "menu:refs")
async def cb_menu_refs(callback: CallbackQuery, state: FSMContext) -> None:
    await asyncio.to_thread(
        ensure_user,
        callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
    )
    await state.set_state(GenFSM.idle)
    await _show_refs_manager(callback.message, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "gen:none")
async def cb_gen_none(callback: CallbackQuery, state: FSMContext) -> None:
    await _begin_prompt_with_refs(
        callback.message,
        state,
        [],
        intro="Ок, генерация без референсов.",
    )
    await callback.answer()


@dp.callback_query(F.data == "gen:pick")
async def cb_gen_pick(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    packs = await asyncio.to_thread(list_packs, user_id)
    if not packs:
        await callback.message.answer(
            "Сохранённых наборов нет. Загрузите новый.",
            reply_markup=generate_refs_keyboard(has_packs=False),
        )
        await callback.answer()
        return
    await state.set_state(GenFSM.choosing_packs)
    await state.update_data(selected_pack_ids=[])
    await callback.message.answer(
        "Выберите один набор референсов.\n"
        f"В наборе может быть до {MAX_REFERENCE_IMAGES} фото.",
        reply_markup=packs_select_keyboard(packs),
    )
    await callback.answer()


@dp.callback_query(GenFSM.choosing_packs, F.data.startswith("pack:"))
async def cb_select_pack(callback: CallbackQuery, state: FSMContext) -> None:
    pack_id = int(callback.data.split(":", 1)[1])
    images = await asyncio.to_thread(
        get_images_for_packs,
        callback.from_user.id,
        [pack_id],
    )
    if not images:
        await callback.answer("Набор не найден или пуст", show_alert=True)
        return

    truncated = len(images) > MAX_REFERENCE_IMAGES
    if truncated:
        images = images[:MAX_REFERENCE_IMAGES]

    title = images[0].get("pack_title") or f"Набор {pack_id}"
    await state.update_data(selected_pack_ids=[pack_id])
    intro = f"Выбран набор «{title}»."
    if truncated:
        intro += f"\nВзяты первые {MAX_REFERENCE_IMAGES} фото (лимит)."

    await _begin_prompt_with_refs(callback.message, state, images, intro=intro)
    await callback.answer()

@dp.callback_query(F.data == "gen:upload")
@dp.callback_query(F.data == "refs:add")
async def cb_start_upload(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    await asyncio.to_thread(
        ensure_user,
        user_id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
    )
    pack_count = await asyncio.to_thread(count_packs, user_id)
    if pack_count >= MAX_REFERENCE_PACKS:
        await callback.message.answer(
            f"Достигнут лимит наборов ({MAX_REFERENCE_PACKS}). "
            "Удалите старый в «Мои референсы»."
        )
        await callback.answer()
        return

    after_save_use = callback.data == "gen:upload"
    await state.set_state(GenFSM.adding_refs)
    await state.update_data(pending_refs=[], after_save_use=after_save_use)
    await callback.message.answer(
        f"Пришлите до {MAX_REFERENCE_IMAGES} фото (можно альбомом).\n"
        "Когда закончите — нажмите «Сохранить набор».",
        reply_markup=save_refs_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("refs:view:"))
async def cb_refs_view(callback: CallbackQuery) -> None:
    pack_id = int(callback.data.split(":")[-1])
    images = await asyncio.to_thread(
        get_images_for_packs,
        callback.from_user.id,
        [pack_id],
    )
    if not images:
        await callback.answer("Набор не найден", show_alert=True)
        return
    title = images[0].get("pack_title") or f"Набор {pack_id}"
    await _resend_reference_photos(
        callback.message,
        images,
        caption=f"{title} · {len(images)} фото",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("refs:del:"))
async def cb_refs_del(callback: CallbackQuery, state: FSMContext) -> None:
    pack_id = int(callback.data.split(":")[-1])
    deleted = await asyncio.to_thread(delete_pack, pack_id, callback.from_user.id)
    if deleted:
        log.info("refs.pack_deleted user=%s pack_id=%s", callback.from_user.id, pack_id)
        await callback.answer("Удалено")
    else:
        await callback.answer("Набор не найден", show_alert=True)
    await state.set_state(GenFSM.idle)
    await _show_refs_manager(callback.message, callback.from_user.id)


@dp.callback_query(F.data.startswith("refs:rename:"))
async def cb_refs_rename(callback: CallbackQuery, state: FSMContext) -> None:
    pack_id = int(callback.data.split(":")[-1])
    packs = await asyncio.to_thread(list_packs, callback.from_user.id)
    pack = next((p for p in packs if p["id"] == pack_id), None)
    if not pack:
        await callback.answer("Набор не найден", show_alert=True)
        return
    await state.set_state(GenFSM.renaming_pack)
    await state.update_data(rename_pack_id=pack_id)
    await callback.message.answer(
        f"Текущее название: «{pack['title']}».\n"
        f"Пришлите новое название (до {MAX_PACK_TITLE_LEN} символов).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:refs")],
            ]
        ),
    )
    await callback.answer()


@dp.message(GenFSM.renaming_pack, F.text)
async def on_rename_pack_title(message: Message, state: FSMContext) -> None:
    title = normalize_pack_title(message.text or "")
    if not title or title.startswith("/"):
        await message.answer(
            f"Нужно текстовое название (1–{MAX_PACK_TITLE_LEN} символов)."
        )
        return
    data = await state.get_data()
    pack_id = data.get("rename_pack_id")
    if not pack_id:
        await _go_idle(message, state, text="Сессия сброшена. Выберите действие:")
        return
    ok = await asyncio.to_thread(rename_pack, int(pack_id), message.from_user.id, title)
    if not ok:
        await message.answer("Не удалось переименовать: набор не найден.")
    else:
        log.info(
            "refs.pack_renamed user=%s pack_id=%s title=%s",
            message.from_user.id,
            pack_id,
            title,
        )
        await message.answer(f"Набор переименован в «{title}».")
    await state.set_state(GenFSM.idle)
    await state.update_data(rename_pack_id=None)
    await _show_refs_manager(message, message.from_user.id)


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


def _pending_added_text(count: int) -> str:
    return (
        f"Фото {count}/{MAX_REFERENCE_IMAGES} в новом наборе.\n"
        "Можете прислать ещё или нажать «Сохранить набор»."
    )


async def _append_pending_and_reply(
    message: Message,
    state: FSMContext,
    new_items: list[dict[str, str]],
) -> None:
    if not new_items:
        return

    user_id = message.from_user.id
    async with _user_photo_locks[user_id]:
        data = await state.get_data()
        pending: list[dict[str, str]] = list(data.get("pending_refs") or [])
        free = MAX_REFERENCE_IMAGES - len(pending)
        if free <= 0:
            await message.answer(
                f"Уже максимум {MAX_REFERENCE_IMAGES} фото. Нажмите «Сохранить набор».",
                reply_markup=save_refs_keyboard(),
            )
            return
        truncated = len(new_items) > free
        if truncated:
            new_items = new_items[:free]
        pending.extend(new_items)
        await state.update_data(pending_refs=pending)
        count = len(pending)

    log.info("refs.pending_added user=%s count=%s added=%s", user_id, count, len(new_items))
    text = _pending_added_text(count)
    if truncated:
        text += f"\nЧасть фото не принята: лимит {MAX_REFERENCE_IMAGES}."
    await message.answer(text, reply_markup=save_refs_keyboard())


async def _finalize_album(album_key: str) -> None:
    try:
        await asyncio.sleep(ALBUM_WAIT_SEC)
    except asyncio.CancelledError:
        return

    async with _album_lock:
        album = _albums.pop(album_key, None)
    if not album or not album.get("items"):
        return

    await _append_pending_and_reply(
        album["message"],
        album["state"],
        list(album["items"]),
    )


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

    item = {
        "telegram_file_id": file_id,
        "public_url": url,
        "local_path": _local_path_from_public_url(url) or "",
    }

    media_group_id = message.media_group_id
    if not media_group_id:
        await _append_pending_and_reply(message, state, [item])
        return

    album_key = f"{message.from_user.id}:{media_group_id}"
    async with _album_lock:
        album = _albums.get(album_key)
        if album is None:
            album = {"items": [], "message": message, "state": state, "task": None}
            _albums[album_key] = album
        album["items"].append(item)
        album["message"] = message
        album["state"] = state
        old_task = album.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        album["task"] = asyncio.create_task(_finalize_album(album_key))


@dp.message(GenFSM.adding_refs, F.photo)
async def on_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    await _handle_incoming_image(
        message,
        state,
        file_id=photo.file_id,
        filename=f"{photo.file_unique_id}.jpg",
        mime="image/jpeg",
    )


@dp.message(GenFSM.adding_refs, F.document)
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


@dp.callback_query(GenFSM.adding_refs, F.data == "refs:save")
async def cb_save_pack(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    for _ in range(10):
        async with _album_lock:
            pending_album = any(k.startswith(f"{user_id}:") for k in _albums)
        if not pending_album:
            break
        await asyncio.sleep(0.4)

    data = await state.get_data()
    pending: list[dict[str, str]] = list(data.get("pending_refs") or [])
    if not pending:
        await callback.answer("Сначала пришлите хотя бы одно фото", show_alert=True)
        return

    pack_count = await asyncio.to_thread(count_packs, user_id)
    if pack_count >= MAX_REFERENCE_PACKS:
        await callback.message.answer(
            f"Лимит наборов ({MAX_REFERENCE_PACKS}). Удалите старый и попробуйте снова."
        )
        await callback.answer()
        return

    auto_title = await asyncio.to_thread(next_pack_title, user_id)
    await state.set_state(GenFSM.naming_pack)
    await callback.message.answer(
        f"Фото готовы: {len(pending)}.\n"
        f"Пришлите название набора (до {MAX_PACK_TITLE_LEN} символов)\n"
        "или нажмите автоназвание.",
        reply_markup=name_pack_keyboard(auto_title=auto_title),
    )
    await callback.answer()


async def _finalize_named_pack(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    title: str,
) -> None:
    data = await state.get_data()
    pending: list[dict[str, str]] = list(data.get("pending_refs") or [])
    if not pending:
        await message.answer("Нет фото для сохранения. Начните заново через «Добавить набор».")
        await state.set_state(GenFSM.idle)
        await _show_refs_manager(message, user_id)
        return

    pack_count = await asyncio.to_thread(count_packs, user_id)
    if pack_count >= MAX_REFERENCE_PACKS:
        await message.answer(
            f"Лимит наборов ({MAX_REFERENCE_PACKS}). Удалите старый и попробуйте снова."
        )
        await state.set_state(GenFSM.idle)
        await _show_refs_manager(message, user_id)
        return

    pack = await asyncio.to_thread(create_pack, user_id, title, pending)
    after_save_use = bool(data.get("after_save_use"))
    log.info(
        "refs.pack_created user=%s pack_id=%s images=%s title=%s use_now=%s",
        user_id,
        pack["id"],
        pack["image_count"],
        title,
        after_save_use,
    )

    await state.update_data(pending_refs=[])
    await message.answer(f"Набор «{title}» сохранён ({pack['image_count']} фото).")

    if after_save_use:
        await _begin_prompt_with_refs(
            message,
            state,
            pack["images"],
            intro=f"Используем только что сохранённый «{title}».",
        )
    else:
        await state.set_state(GenFSM.idle)
        await _show_refs_manager(message, user_id)


@dp.callback_query(GenFSM.naming_pack, F.data == "refs:autoname")
async def cb_auto_name_pack(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    title = await asyncio.to_thread(next_pack_title, user_id)
    await _finalize_named_pack(callback.message, state, user_id=user_id, title=title)
    await callback.answer("Сохранено")


@dp.message(GenFSM.naming_pack, F.text)
async def on_pack_title(message: Message, state: FSMContext) -> None:
    title = normalize_pack_title(message.text or "")
    if not title or title.startswith("/"):
        auto_title = await asyncio.to_thread(next_pack_title, message.from_user.id)
        await message.answer(
            f"Нужно текстовое название (1–{MAX_PACK_TITLE_LEN} символов).\n"
            "Или нажмите автоназвание.",
            reply_markup=name_pack_keyboard(auto_title=auto_title),
        )
        return
    await _finalize_named_pack(
        message,
        state,
        user_id=message.from_user.id,
        title=title,
    )

@dp.message(GenFSM.awaiting_prompt, F.text)
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

    data = await state.get_data()
    refs = data.get("active_refs") or []
    await state.update_data(prompt=prompt)
    await state.set_state(GenFSM.choose_resolution)
    log.info(
        "prompt.accepted user=%s refs=%s prompt_len=%s left=%s",
        user_id,
        len(refs),
        len(prompt),
        left,
    )
    await message.answer(
        f"Промпт принят. Референсов: {len(refs)}.\n"
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
    refs = data.get("active_refs") or []
    photos = [item["public_url"] for item in refs if item.get("public_url")]

    if not prompt:
        await callback.message.answer("Промпт потерян. Нажмите /new и начните снова.")
        await _go_idle(callback.message, state)
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
        await _go_idle(callback.message, state)
        await callback.answer()
        return

    await state.set_state(GenFSM.idle)
    await state.update_data(prompt=None, active_refs=[], selected_pack_ids=[])
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
        await callback.message.answer("Выберите действие:", reply_markup=main_menu_keyboard())
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
    await callback.message.answer(
        "Можно сделать ещё одну генерацию — референсы уже сохранены.",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(F.photo)
@dp.message(F.document)
async def fallback_media(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current == GenFSM.adding_refs.state:
        return
    await message.answer(
        "Чтобы сохранить фото как референсы, откройте «Мои референсы» → «Добавить набор» "
        "или «Новая генерация» → «Загрузить новый набор».",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(F.text)
async def fallback_text(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None or current == GenFSM.idle.state:
        await message.answer(
            "Выберите действие в меню или нажмите /start.",
            reply_markup=main_menu_keyboard(),
        )
        return
    if current == GenFSM.adding_refs.state:
        await message.answer(
            "Сейчас ждём фото для набора. Пришлите изображения или нажмите «Сохранить набор».",
            reply_markup=save_refs_keyboard(),
        )
        return
    if current == GenFSM.naming_pack.state:
        auto_title = await asyncio.to_thread(next_pack_title, message.from_user.id)
        await message.answer(
            f"Пришлите название набора текстом (до {MAX_PACK_TITLE_LEN} символов)\n"
            "или нажмите автоназвание.",
            reply_markup=name_pack_keyboard(auto_title=auto_title),
        )
        return
    if current == GenFSM.renaming_pack.state:
        await message.answer(
            f"Пришлите новое название набора текстом (до {MAX_PACK_TITLE_LEN} символов)."
        )
        return
    if current == GenFSM.choosing_packs.state:
        await message.answer("Выберите один набор кнопкой выше.")
        return
    if current in {GenFSM.choose_resolution.state, GenFSM.choose_aspect.state}:
        await message.answer("Выберите вариант кнопками выше или /cancel.")


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
