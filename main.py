from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from .expense_file import ExpenseFile, ExpenseFileError, TextExpense
from .keyboards import back_keyboard, main_menu_keyboard, reminder_keyboard
from .reminders import (
    MOSCOW_TZ,
    Reminder,
    ReminderFileError,
    ReminderScheduler,
    parse_moscow_datetime,
    reminder_check_loop,
)
from .states import ReminderFlow


BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_DESCRIPTION_LENGTH = 1000
MAX_REPLY_LENGTH = 3800

logger = logging.getLogger(__name__)
dispatcher = Dispatcher(storage=MemoryStorage())
expenses = ExpenseFile()
reminders = ReminderScheduler()
HELP_TEXT = (
    "<b>❓ Помощь</b>\n\n"
    "<b>Напоминания</b>\n"
    "Нажмите «Добавить напоминание» и следуйте шагам. "
    "Дата и время вводятся в формате ДД.ММ.ГГГГ ЧЧ:ММ.\n"
    "Также можно использовать: <code>/remind 2026-11-15 14:30 Позвонить маме</code>\n"
    "Нажмите «Мои напоминания», чтобы посмотреть, изменить или удалить запись.\n\n"
    "<b>Команды</b>\n"
    "/start или /menu — главное меню\n"
    "/reminders — список напоминаний\n"
    "/remind ДД.ММ.ГГГГ ЧЧ:ММ текст — добавить напоминание\n"
    "/help — эта справка\n\n"
    "<b>Расходы</b>\n"
    "/add сумма описание — добавить расход\n"
    "/list — список расходов\n"
    "/total — сумма расходов\n"
    "/del — очистить ваши расходы"
)


def _private_user_id(message: Message) -> int | None:
    if message.chat.type != "private" or message.from_user is None:
        return None
    return message.from_user.id


def _format_amount(amount: Decimal) -> str:
    return format(amount.normalize(), "f")


def _format_expense(expense: TextExpense) -> str:
    return f"{_format_amount(expense.amount)} — {expense.description}"


def _expense_chunks(records: Iterable[TextExpense]) -> list[str]:
    chunks: list[str] = []
    current = "Your expenses:\n"

    for record in records:
        line = _format_expense(record)
        if len(current) + len(line) + 1 > MAX_REPLY_LENGTH:
            chunks.append(current.rstrip())
            current = "Your expenses (continued):\n"
        current += f"{line}\n"

    if current.strip():
        chunks.append(current.rstrip())
    return chunks


async def _reply_private_chat_required(message: Message) -> int | None:
    user_id = _private_user_id(message)
    if user_id is None:
        await message.answer(
            "Please use this bot in a private chat.",
            reply_markup=back_keyboard(),
        )
    return user_id


def _private_callback_chat_id(callback: CallbackQuery) -> int | None:
    if (
        isinstance(callback.message, Message)
        and callback.message.chat.type == "private"
        and callback.from_user.id == callback.message.chat.id
    ):
        return callback.message.chat.id
    return None


def _format_reminder(reminder: Reminder) -> str:
    return (
        f"⏰ {reminder.remind_at.strftime('%d.%m.%Y в %H:%M')}\n"
        f"{reminder.message_text}"
    )


async def _send_reminder_list(message: Message, chat_id: int) -> None:
    try:
        user_reminders = await reminders.list_for(chat_id)
    except ReminderFileError:
        logger.error("Could not read reminders.txt.")
        await message.answer(
            "Не удалось прочитать напоминания.",
            reply_markup=back_keyboard(),
        )
        return

    if not user_reminders:
        await message.answer(
            "У вас нет напоминаний",
            reply_markup=back_keyboard("⬅️ Назад"),
        )
        return

    await message.answer("📋 Ваши напоминания:", reply_markup=back_keyboard())
    for reminder in user_reminders:
        await message.answer(
            _format_reminder(reminder),
            reply_markup=reminder_keyboard(reminder),
        )


async def _report_file_error(message: Message, error: Exception) -> None:
    logger.exception("Could not process expenses.txt", exc_info=error)
    await message.answer(
        "I couldn't read or update expenses.txt. Please check the file and try again.",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await _reply_private_chat_required(message) is None:
        return
    await message.answer("👋 Главное меню", reply_markup=main_menu_keyboard())


@dispatcher.message(Command("menu"))
async def show_menu_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await _reply_private_chat_required(message) is None:
        return
    await message.answer("👋 Главное меню", reply_markup=main_menu_keyboard())


@dispatcher.message(Command("help"))
async def show_help_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await _reply_private_chat_required(message) is None:
        return
    await message.answer(
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(Command("reminders"))
async def show_reminders_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    chat_id = await _reply_private_chat_required(message)
    if chat_id is None:
        return
    await _send_reminder_list(message, chat_id)


@dispatcher.message(Command("add"))
async def add_expense(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return

    arguments = (command.args or "").strip()
    try:
        amount_text, description = arguments.split(maxsplit=1)
    except ValueError:
        await message.answer(
            "Usage: /add [amount] [description]\nExample: /add 12.50 lunch",
            reply_markup=back_keyboard(),
        )
        return

    try:
        amount = Decimal(amount_text)
    except InvalidOperation:
        await message.answer(
            "Enter a valid positive amount, for example: /add 12.50 lunch",
            reply_markup=back_keyboard(),
        )
        return

    description = " ".join(description.split())
    if not amount.is_finite() or amount <= 0:
        await message.answer("The amount must be greater than zero.", reply_markup=back_keyboard())
        return
    if not description:
        await message.answer("Please include a description.", reply_markup=back_keyboard())
        return
    if len(description) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"Please keep descriptions under {MAX_DESCRIPTION_LENGTH} characters.",
            reply_markup=back_keyboard(),
        )
        return

    try:
        expenses.add(user_id, amount, description)
    except (ExpenseFileError, OSError) as error:
        await _report_file_error(message, error)
        return
    await message.answer(
        f"Saved {_format_amount(amount)} — {description}",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(Command("list"))
async def list_expenses(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return
    try:
        records = expenses.list_for(user_id)
    except (ExpenseFileError, OSError) as error:
        await _report_file_error(message, error)
        return

    if not records:
        await message.answer(
            "You don't have any expenses yet.",
            reply_markup=back_keyboard(),
        )
        return
    for chunk in _expense_chunks(records):
        await message.answer(chunk, reply_markup=back_keyboard())


@dispatcher.message(Command("total"))
async def total_expenses(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return
    try:
        records = expenses.list_for(user_id)
    except (ExpenseFileError, OSError) as error:
        await _report_file_error(message, error)
        return

    total = sum((record.amount for record in records), Decimal("0"))
    await message.answer(
        f"Your total is {_format_amount(total)}.",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(Command("del"))
async def delete_expenses(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return
    try:
        removed_count = expenses.clear_for(user_id)
    except (ExpenseFileError, OSError) as error:
        await _report_file_error(message, error)
        return

    if removed_count == 0:
        await message.answer(
            "You don't have any expenses to clear.",
            reply_markup=back_keyboard(),
        )
        return
    await message.answer(
        f"Cleared your {removed_count} expense(s).",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(Command("remind"))
async def add_reminder_command(
    message: Message,
    command: CommandObject,
    state: FSMContext,
) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return

    arguments = (command.args or "").strip()
    parts = arguments.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Usage: /remind YYYY-MM-DD HH:MM message\n"
            "Example: /remind 2026-11-15 14:30 Позвонить маме",
            reply_markup=back_keyboard(),
        )
        return

    date_text, time_text, message_text = parts
    try:
        remind_at = parse_moscow_datetime(date_text, time_text, allow_iso_date=True)
    except ValueError:
        await message.answer(
            "Неверный формат даты или времени. Используйте YYYY-MM-DD HH:MM "
            "или ДД.ММ.ГГГГ ЧЧ:ММ.",
            reply_markup=back_keyboard(),
        )
        return

    message_text = " ".join(message_text.split())
    if not message_text:
        await message.answer(
            "Please include a reminder message.\n"
            "Example: /remind 2026-11-15 14:30 Позвонить маме",
            reply_markup=back_keyboard(),
        )
        return
    if remind_at <= datetime.now(MOSCOW_TZ):
        await message.answer(
            "Please choose a future date and time in Moscow time (UTC+3).",
            reply_markup=back_keyboard(),
        )
        return

    reminder = Reminder(
        chat_id=user_id,
        remind_at=remind_at,
        message_text=message_text,
    )
    try:
        await reminders.add(reminder)
    except ReminderFileError:
        logger.error("Could not save a reminder to reminders.txt.")
        await message.answer(
            "I couldn't save the reminder. Please check reminders.txt and try again.",
            reply_markup=back_keyboard(),
        )
        return

    await message.answer(
        f"✅ Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}: {message_text}",
        reply_markup=main_menu_keyboard(),
    )


@dispatcher.callback_query(F.data == "add_reminder")
async def begin_reminder_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    if _private_callback_chat_id(callback) is None:
        return
    await state.set_state(ReminderFlow.waiting_for_text)
    await callback.message.edit_text(
        "Введите текст напоминания:",
        reply_markup=back_keyboard(),
    )


@dispatcher.callback_query(F.data == "list_reminders")
async def list_reminders_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    chat_id = _private_callback_chat_id(callback)
    if chat_id is None:
        return

    try:
        user_reminders = await reminders.list_for(chat_id)
    except ReminderFileError:
        logger.error("Could not read reminders.txt.")
        await callback.message.edit_text(
            "Не удалось прочитать напоминания.",
            reply_markup=back_keyboard(),
        )
        return

    if not user_reminders:
        await callback.message.edit_text(
            "У вас нет напоминаний",
            reply_markup=back_keyboard("⬅️ Назад"),
        )
        return

    await callback.message.edit_text(
        "📋 Ваши напоминания:",
        reply_markup=back_keyboard(),
    )
    for reminder in user_reminders:
        await callback.message.answer(
            _format_reminder(reminder),
            reply_markup=reminder_keyboard(reminder),
        )


@dispatcher.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    if _private_callback_chat_id(callback) is None:
        return
    await callback.message.edit_text(
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


@dispatcher.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    if _private_callback_chat_id(callback) is None:
        return
    await callback.message.edit_text(
        "👋 Главное меню",
        reply_markup=main_menu_keyboard(),
    )


@dispatcher.callback_query(F.data.startswith("delete_reminder:"))
async def delete_reminder_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    chat_id = _private_callback_chat_id(callback)
    if chat_id is None:
        return
    key = (callback.data or "").partition(":")[2]
    try:
        deleted = await reminders.delete(chat_id, key)
    except ReminderFileError:
        logger.error("Could not update reminders.txt after deleting a reminder.")
        await callback.message.edit_text(
            "Не удалось удалить напоминание.",
            reply_markup=back_keyboard(),
        )
        return
    await callback.message.edit_text(
        "🗑 Напоминание удалено." if deleted else "Напоминание уже не найдено.",
        reply_markup=back_keyboard(),
    )


@dispatcher.callback_query(F.data.startswith("edit_reminder:"))
async def edit_reminder_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    chat_id = _private_callback_chat_id(callback)
    if chat_id is None:
        return
    key = (callback.data or "").partition(":")[2]
    reminder = await reminders.get_by_key(chat_id, key)
    if reminder is None:
        await callback.message.edit_text(
            "Напоминание уже не найдено.",
            reply_markup=back_keyboard(),
        )
        return

    await state.set_data({"edit_key": key})
    await state.set_state(ReminderFlow.waiting_for_text)
    await callback.message.edit_text(
        "Введите новый текст напоминания:",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(ReminderFlow.waiting_for_text, F.text, ~F.text.startswith("/"))
async def receive_reminder_text(message: Message, state: FSMContext) -> None:
    if await _reply_private_chat_required(message) is None:
        await state.clear()
        return

    text = " ".join((message.text or "").split())
    if not text:
        await message.answer(
            "Введите текст напоминания:",
            reply_markup=back_keyboard(),
        )
        return
    if len(text) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"Текст должен быть короче {MAX_DESCRIPTION_LENGTH} символов.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(pending_message=text)
    await state.set_state(ReminderFlow.waiting_for_datetime)
    await message.answer(
        "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ "
        "(например, 15.11.2026 14:30):",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(ReminderFlow.waiting_for_datetime, F.text, ~F.text.startswith("/"))
async def receive_reminder_datetime(message: Message, state: FSMContext) -> None:
    chat_id = await _reply_private_chat_required(message)
    if chat_id is None:
        await state.clear()
        return

    parts = (message.text or "").strip().split()
    if len(parts) != 2:
        await message.answer(
            "Неверный формат. Введите ДД.ММ.ГГГГ ЧЧ:ММ, например 15.11.2026 14:30:",
            reply_markup=back_keyboard(),
        )
        return
    try:
        remind_at = parse_moscow_datetime(parts[0], parts[1])
    except ValueError:
        await message.answer(
            "Неверная дата или время. Используйте ДД.ММ.ГГГГ ЧЧ:ММ, "
            "например 15.11.2026 14:30:",
            reply_markup=back_keyboard(),
        )
        return
    if remind_at <= datetime.now(MOSCOW_TZ):
        await message.answer(
            "Укажите дату и время в будущем по московскому времени (UTC+3).",
            reply_markup=back_keyboard(),
        )
        return

    state_data = await state.get_data()
    message_text = state_data.get("pending_message", "")
    edit_key = state_data.get("edit_key")
    try:
        if edit_key:
            saved_reminder = await reminders.edit(
                chat_id,
                edit_key,
                remind_at,
                message_text,
            )
            if saved_reminder is None:
                await state.clear()
                await message.answer(
                    "Напоминание уже не найдено.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            confirmation = (
                f"✅ Напоминание изменено. Напомню "
                f"{remind_at.strftime('%d.%m.%Y в %H:%M')}: {message_text}"
            )
        else:
            await reminders.add(
                Reminder(
                    chat_id=chat_id,
                    remind_at=remind_at,
                    message_text=message_text,
                )
            )
            confirmation = (
                f"✅ Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}: "
                f"{message_text}"
            )
    except ReminderFileError:
        logger.error("Could not save a reminder to reminders.txt.")
        await message.answer(
            "Не удалось сохранить напоминание. Попробуйте ещё раз:",
            reply_markup=back_keyboard(),
        )
        return

    await state.clear()
    await message.answer(confirmation, reply_markup=main_menu_keyboard())


@dispatcher.message(ReminderFlow.waiting_for_text)
async def reminder_text_required(message: Message) -> None:
    await message.answer(
        "Введите текст напоминания:",
        reply_markup=back_keyboard(),
    )


@dispatcher.message(ReminderFlow.waiting_for_datetime)
async def reminder_datetime_required(message: Message) -> None:
    await message.answer(
        "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ "
        "(например, 15.11.2026 14:30):",
        reply_markup=back_keyboard(),
    )


async def _run_bot() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to Replit Secrets before starting the bot.")
    reminders.load()
    async with Bot(token=BOT_TOKEN) as bot:
        reminder_task = asyncio.create_task(
            reminder_check_loop(
                reminders,
                bot,
                reply_markup=main_menu_keyboard(),
            ),
            name="reminder-check-loop",
        )
        try:
            await dispatcher.start_polling(bot)
        finally:
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                pass


def main() -> None:
    try:
        asyncio.run(_run_bot())
    except KeyboardInterrupt:
        logger.info("Expenses bot stopped.")


if __name__ == "__main__":
    main()
