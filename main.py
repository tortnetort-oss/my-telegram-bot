from __future__ import annotations
import asyncio
import logging
import os
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence, Iterable
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import threading

# ==================== КОНФИГ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MAX_DESCRIPTION_LENGTH = 1000
MAX_REPLY_LENGTH = 3800

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ==================== ХРАНИЛИЩЕ РАСХОДОВ ====================
EXPENSES_FILE = "expenses.json"

class TextExpense:
    def __init__(self, id: int, amount: Decimal, description: str, spent_on: str):
        self.id = id
        self.amount = amount
        self.description = description
        self.spent_on = spent_on

class ExpenseFile:
    def __init__(self):
        self.file = Path(EXPENSES_FILE)
        if not self.file.exists():
            self.file.write_text("{}")
    
    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except:
            return {}
    
    def _save(self, data: dict):
        self.file.write_text(json.dumps(data, indent=2))
    
    def add(self, user_id: int, amount: Decimal, description: str, date=None):
        data = self._load()
        if str(user_id) not in data:
            data[str(user_id)] = []
        expense = {
            "id": len(data[str(user_id)]) + 1,
            "amount": str(amount),
            "description": description,
            "date": date.isoformat() if date else datetime.now().isoformat()
        }
        data[str(user_id)].append(expense)
        self._save(data)
        return expense["id"]
    
    def list_for(self, user_id: int) -> list:
        data = self._load()
        expenses = data.get(str(user_id), [])
        return [TextExpense(e["id"], Decimal(e["amount"]), e["description"], e["date"]) for e in expenses]
    
    def clear_for(self, user_id: int) -> int:
        data = self._load()
        count = len(data.get(str(user_id), []))
        if str(user_id) in data:
            data[str(user_id)] = []
            self._save(data)
        return count

# ==================== НАПОМИНАНИЯ ====================
REMINDERS_FILE = "reminders.json"

class Reminder:
    def __init__(self, chat_id: int, remind_at: datetime, message_text: str):
        self.chat_id = chat_id
        self.remind_at = remind_at
        self.message_text = message_text
        self.key = f"{chat_id}:{remind_at.isoformat()}:{message_text[:20]}"

class ReminderScheduler:
    def __init__(self):
        self.file = Path(REMINDERS_FILE)
        if not self.file.exists():
            self.file.write_text("{}")
        self.reminders: dict = {}
    
    def load(self):
        try:
            data = json.loads(self.file.read_text())
            for chat_id_str, reminders_list in data.items():
                for r in reminders_list:
                    remind_at = datetime.fromisoformat(r["remind_at"])
                    reminder = Reminder(int(chat_id_str), remind_at, r["message_text"])
                    self.reminders[reminder.key] = reminder
        except:
            pass
    
    async def add(self, reminder: Reminder):
        self.reminders[reminder.key] = reminder
        self._save()
    
    async def list_for(self, chat_id: int) -> list:
        return [r for r in self.reminders.values() if r.chat_id == chat_id]
    
    async def delete(self, chat_id: int, key: str) -> bool:
        full_key = f"{chat_id}:{key}" if ":" not in key else key
        if full_key in self.reminders:
            del self.reminders[full_key]
            self._save()
            return True
        return False
    
    def _save(self):
        data = {}
        for reminder in self.reminders.values():
            if str(reminder.chat_id) not in data:
                data[str(reminder.chat_id)] = []
            data[str(reminder.chat_id)].append({
                "remind_at": reminder.remind_at.isoformat(),
                "message_text": reminder.message_text
            })
        self.file.write_text(json.dumps(data, indent=2))

# ==================== КЛАВИАТУРЫ ====================
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить напоминание", callback_data="add_reminder")],
        [InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])

def back_keyboard(text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="main_menu")]
    ])

def reminder_keyboard(reminder: Reminder) -> InlineKeyboardMarkup:
    key = reminder.key.split(":", 2)[-1] if ":" in reminder.key else reminder.key
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_reminder:{key}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_reminder:{key}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ])

# ==================== FSM STATES ====================
class ReminderFlow:
    waiting_for_text = "waiting_for_text"
    waiting_for_datetime = "waiting_for_datetime"

# ==================== БОТ ====================
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
    current = "Ваши расходы:\n"
    for record in records:
        line = _format_expense(record)
        if len(current) + len(line) + 1 > MAX_REPLY_LENGTH:
            chunks.append(current.rstrip())
            current = "Ваши расходы (продолжение):\n"
        current += f"{line}\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks

async def _reply_private_chat_required(message: Message) -> int | None:
    user_id = _private_user_id(message)
    if user_id is None:
        await message.answer("Пожалуйста, используйте бота в личных сообщениях.", reply_markup=back_keyboard())
    return user_id

def _format_reminder(reminder: Reminder) -> str:
    return f"⏰ {reminder.remind_at.strftime('%d.%m.%Y в %H:%M')}\n{reminder.message_text}"

async def reminder_check_loop(reminders_scheduler: ReminderScheduler, bot: Bot, reply_markup):
    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            to_send = []
            for key, reminder in list(reminders_scheduler.reminders.items()):
                if reminder.remind_at <= now:
                    to_send.append(reminder)
            for reminder in to_send:
                try:
                    await bot.send_message(
                        reminder.chat_id,
                        f"🔔 Напоминание:\n{reminder.message_text}",
                        reply_markup=reply_markup
                    )
                    await reminders_scheduler.delete(reminder.chat_id, reminder.key)
                except Exception as e:
                    logger.error(f"Failed to send reminder: {e}")
        except Exception as e:
            logger.error(f"Reminder check error: {e}")
        await asyncio.sleep(30)

@dispatcher.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await _reply_private_chat_required(message) is None:
        return
    await message.answer(" Главное меню", reply_markup=main_menu_keyboard())

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
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=back_keyboard())

@dispatcher.message(Command("reminders"))
async def show_reminders_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    chat_id = await _reply_private_chat_required(message)
    if chat_id is None:
        return
    user_reminders = await reminders.list_for(chat_id)
    if not user_reminders:
        await message.answer("У вас нет напоминаний", reply_markup=back_keyboard("⬅️ Назад"))
        return
    await message.answer("📋 Ваши напоминания:", reply_markup=back_keyboard())
    for reminder in user_reminders:
        await message.answer(_format_reminder(reminder), reply_markup=reminder_keyboard(reminder))

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
        await message.answer("Использование: /add [сумма] [описание]\nПример: /add 12.50 обед", reply_markup=back_keyboard())
        return
    try:
        amount = Decimal(amount_text)
    except InvalidOperation:
        await message.answer("Введите корректную сумму, например: /add 12.50 обед", reply_markup=back_keyboard())
        return
    if not amount.is_finite() or amount <= 0:
        await message.answer("Сумма должна быть больше нуля.", reply_markup=back_keyboard())
        return
    if not description:
        await message.answer("Пожалуйста, добавьте описание.", reply_markup=back_keyboard())
        return
    try:
        expenses.add(user_id, amount, description)
    except Exception as error:
        await message.answer(f"Ошибка при сохранении: {error}", reply_markup=back_keyboard())
        return
    await message.answer(f"Сохранено {_format_amount(amount)} — {description}", reply_markup=back_keyboard())

@dispatcher.message(Command("list"))
async def list_expenses(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return
    try:
        records = expenses.list_for(user_id)
    except Exception as error:
        await message.answer(f"Ошибка при чтении: {error}", reply_markup=back_keyboard())
        return
    if not records:
        await message.answer("У вас пока нет расходов.", reply_markup=back_keyboard())
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
    except Exception as error:
        await message.answer(f"Ошибка при чтении: {error}", reply_markup=back_keyboard())
        return
    total = sum((record.amount for record in records), Decimal("0"))
    await message.answer(f"Всего потрачено: {_format_amount(total)} ₽", reply_markup=back_keyboard())

@dispatcher.message(Command("del"))
async def delete_expenses(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return
    try:
        removed_count = expenses.clear_for(user_id)
    except Exception as error:
        await message.answer(f"Ошибка: {error}", reply_markup=back_keyboard())
        return
    if removed_count == 0:
        await message.answer("У вас нет расходов для очистки.", reply_markup=back_keyboard())
        return
    await message.answer(f"Удалено расходов: {removed_count}", reply_markup=back_keyboard())

@dispatcher.message(Command("remind"))
async def add_reminder_command(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    user_id = await _reply_private_chat_required(message)
    if user_id is None:
        return
    arguments = (command.args or "").strip()
    parts = arguments.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /remind ДД.ММ.ГГГГ ЧЧ:ММ сообщение\nПример: /remind 15.11.2026 14:30 Позвонить маме", reply_markup=back_keyboard())
        return
    date_text, time_text, message_text = parts
    try:
        date_part, time_part = date_text.split(".") if "." in date_text else (date_text, time_text)
        if "." in date_text:
            day, month, year = date_text.split(".")
            remind_at = datetime(int(year), int(month), int(day), int(time_text.split(":")[0]), int(time_text.split(":")[1]), tzinfo=MOSCOW_TZ)
        else:
            remind_at = datetime.fromisoformat(f"{date_text} {time_text}").replace(tzinfo=MOSCOW_TZ)
    except Exception:
        await message.answer("Неверный формат. Используйте ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=back_keyboard())
        return
    if remind_at <= datetime.now(MOSCOW_TZ):
        await message.answer("Укажите будущее время.", reply_markup=back_keyboard())
        return
    reminder = Reminder(chat_id=user_id, remind_at=remind_at, message_text=message_text)
    try:
        await reminders.add(reminder)
    except Exception:
        await message.answer("Не удалось сохранить напоминание.", reply_markup=back_keyboard())
        return
    await message.answer(f"✅ Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}: {message_text}", reply_markup=main_menu_keyboard())

@dispatcher.callback_query(F.data == "add_reminder")
async def begin_reminder_flow(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    await state.set_state(ReminderFlow.waiting_for_text)
    await callback.message.edit_text("Введите текст напоминания:", reply_markup=back_keyboard())

@dispatcher.callback_query(F.data == "list_reminders")
async def list_reminders_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    chat_id = callback.message.chat.id
    user_reminders = await reminders.list_for(chat_id)
    if not user_reminders:
        await callback.message.edit_text("У вас нет напоминаний", reply_markup=back_keyboard("⬅️ Назад"))
        return
    await callback.message.edit_text("📋 Ваши напоминания:", reply_markup=back_keyboard())
    for reminder in user_reminders:
        await callback.message.answer(_format_reminder(reminder), reply_markup=reminder_keyboard(reminder))

@dispatcher.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    await callback.message.edit_text(HELP_TEXT, parse_mode="HTML", reply_markup=back_keyboard())

@dispatcher.callback_query(F.data == "main_menu")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    await callback.message.edit_text("👋 Главное меню", reply_markup=main_menu_keyboard())

@dispatcher.callback_query(F.data.startswith("delete_reminder:"))
async def delete_reminder_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    chat_id = callback.message.chat.id
    key = (callback.data or "").partition(":")[2]
    deleted = await reminders.delete(chat_id, key)
    await callback.message.edit_text("🗑 Напоминание удалено." if deleted else "Напоминание уже не найдено.", reply_markup=back_keyboard())

@dispatcher.callback_query(F.data.startswith("edit_reminder:"))
async def edit_reminder_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    await state.set_data({"edit_key": (callback.data or "").partition(":")[2]})
    await state.set_state(ReminderFlow.waiting_for_text)
    await callback.message.edit_text("Введите новый текст напоминания:", reply_markup=back_keyboard())

@dispatcher.message(ReminderFlow.waiting_for_text, F.text, ~F.text.startswith("/"))
async def receive_reminder_text(message: Message, state: FSMContext) -> None:
    if await _reply_private_chat_required(message) is None:
        await state.clear()
        return
    text = " ".join((message.text or "").split())
    if not text:
        await message.answer("Введите текст напоминания:", reply_markup=back_keyboard())
        return
    await state.update_data(pending_message=text)
    await state.set_state(ReminderFlow.waiting_for_datetime)
    await message.answer("Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ (например, 15.11.2026 14:30):", reply_markup=back_keyboard())

@dispatcher.message(ReminderFlow.waiting_for_datetime, F.text, ~F.text.startswith("/"))
async def receive_reminder_datetime(message: Message, state: FSMContext) -> None:
    chat_id = await _reply_private_chat_required(message)
    if chat_id is None:
        await state.clear()
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 2:
        await message.answer("Неверный формат. Введите ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=back_keyboard())
        return
    try:
        day, month, year = parts[0].split(".")
        hour, minute = parts[1].split(":")
        remind_at = datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=MOSCOW_TZ)
    except Exception:
        await message.answer("Неверная дата или время. Используйте ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=back_keyboard())
        return
    if remind_at <= datetime.now(MOSCOW_TZ):
        await message.answer("Укажите будущее время.", reply_markup=back_keyboard())
        return
    state_data = await state.get_data()
    message_text = state_data.get("pending_message", "")
    reminder = Reminder(chat_id=chat_id, remind_at=remind_at, message_text=message_text)
    try:
        await reminders.add(reminder)
    except Exception:
        await message.answer("Не удалось сохранить напоминание.", reply_markup=back_keyboard())
        return
    await state.clear()
    await message.answer(f"✅ Напомню {remind_at.strftime('%d.%m.%Y в %H:%M')}: {message_text}", reply_markup=main_menu_keyboard())

# ==================== ВЕБ-СЕРВЕР (для Render) ====================
async def handle(request):
    return web.Response(text="Бот работает! 🤖")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 3000)
    await site.start()
    logger.info("Web server started on port 3000")

def run_web_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_web_server())

# ==================== ЗАПУСК ====================
async def _run_bot() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to Render Environment Variables!")
    reminders.load()
    async with Bot(token=BOT_TOKEN) as bot:
        reminder_task = asyncio.create_task(
            reminder_check_loop(reminders, bot, main_menu_keyboard()),
            name="reminder-check-loop"
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
    # Запускаем веб-сервер в отдельном потоке
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()
    try:
        asyncio.run(_run_bot())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    main()
