import asyncio
import random
import logging
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart, Command
from config import OWNER_ID, MIN_ORDER_USD, MAX_ORDER_USD, WORK_HOURS_START, WORK_HOURS_END
from db.database import (
    get_rate, set_rate, add_admin, is_admin, create_order, get_order, update_order_status,
    add_check_account, list_check_accounts, delete_check_account,
    create_check_request, get_check_request_by_manager_msg, add_balance, get_balance
)
from bot.states import OrderForm, AdminForm, DepositForm
from bot.keyboards import (
    main_menu, admin_menu, check_accounts_menu, del_acc_list,
    order_actions, order_after_accept, payment_status, video_verification
)
from services.checks import background_verify

router = Router()
log = logging.getLogger(__name__)

def is_work_time():
    now = datetime.now()
    return WORK_HOURS_START <= now.hour < WORK_HOURS_END

async def get_managers(bot: Bot):
    """Все менеджеры: владелец + добавленные админы."""
    ids = [OWNER_ID]
    import aiosqlite
    from db.database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cur:
            async for row in cur:
                if row[0] not in ids:
                    ids.append(row[0])
    return ids

# ---------- START ----------
@router.message(CommandStart())
async def start(message: Message):
    if not is_work_time():
        await message.answer(
            f"⏰ Обменник работает с {WORK_HOURS_START}:00 до {WORK_HOURS_END}:00. Заходите позже.")
        return
    await message.answer("Добро пожаловать в TwaragaSwap!", reply_markup=main_menu())

@router.callback_query(F.data == "rate")
async def show_rate(cb: CallbackQuery):
    r = await get_rate()
    await cb.message.edit_text(f"📊 Курс: 1 USD = {r} RUB")
    await cb.answer()

@router.callback_query(F.data == "balance")
async def show_balance(cb: CallbackQuery):
    b = await get_balance(cb.from_user.id)
    await cb.message.edit_text(f"💼 Ваш баланс: {b:.2f} ₽")
    await cb.answer()

# ---------- ПОПОЛНЕНИЕ ЧЕКОМ ----------
@router.callback_query(F.data == "deposit")
async def deposit_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(DepositForm.waiting_cheque)
    await cb.message.edit_text(
        "💳 Отправьте чек CryptoBot или xRocket.\n\n"
        "Можно отправить:\n"
        "• как файл / чек (пересылку)\n"
        "• ссылку на чек\n"
        "• текст с ID/ссылкой чека\n\n"
        "Менеджер ответит на ваш чек суммой и зачислит средства.")
    await cb.answer()

@router.message(DepositForm.waiting_cheque)
async def deposit_receive(message: Message, state: FSMContext, bot: Bot):
    text = message.text or message.caption or ""
    if not text:
        await message.answer("❌ Отправьте чек текстом или как чек.")
        return

    # Определяем сервис по ссылке
    service = None
    low = text.lower()
    if "cryptobot" in low or "send?start=" in low or "t.me/CryptoBot" in text:
        service = "cryptobot"
    elif "xrocket" in low or "t.me/xRocket" in text:
        service = "xrocket"
    else:
        # если не распознали — пусть менеджер уточнит
        service = "unknown"

    # пересылка менеджерам
    managers = await get_managers(bot)
    for mgr in managers:
        try:
            fwd = await bot.forward_message(chat_id=mgr, from_chat_id=message.chat.id,
                                            message_id=message.message_id)
            await bot.send_message(
                mgr,
                f"🔔 Новый чек на пополнение от @{message.from_user.username or message.from_user.id}\n"
                f"👤 User ID: {message.from_user.id}\n"
                f"🏦 Сервис: {service}\n\n"
                f"💬 Ответьте РЕПЛАЕМ на сообщение с чеком и введите сумму ₽:",
                reply_to_message_id=fwd.message_id
            )
        except Exception as e:
            log.warning(f"forward to {mgr} failed: {e}")

    await state.clear()
    await message.answer("✅ Чек отправлен менеджеру. Ожидайте подтверждения суммы.")

# ---------- ОТВЕТ МЕНЕДЖЕРА СУММОЙ (реплай на чек) ----------
@router.message(F.reply_to_message, F.from_user.id.in_([OWNER_ID]))
async def manager_reply_amount_placeholder(message: Message):
    # заглушка, чтобы не срабатывали другие хэндлеры
    pass

@router.message(F.reply_to_message)
async def manager_reply_amount(message: Message, bot: Bot):
    """Менеджер ответил реплаем на пересланный чек — вводит сумму."""
    # проверка: действительно ли это менеджер
    if message.from_user.id != OWNER_ID and not await is_admin(message.from_user.id):
        return

    # реплай должен быть на сообщение-чек
    replied = message.reply_to_message
    # Проверим, есть ли в replied текст с "Новый чек"
    if not replied or not replied.text or "Новый чек" not in replied.text:
        return

    try:
        amount = float(message.text.replace(",", ".").strip())
    except (ValueError, AttributeError):
        await message.answer("❌ Введите сумму числом (например 3000).")
        return

    if amount <= 0 or amount > 1_000_000:
        await message.answer("❌ Некорректная сумма. Проверьте ввод.")
        return

    # парсим user_id и service из текста replied
    import re
    user_id_match = re.search(r"User ID:\s*(\d+)", replied.text)
    service_match = re.search(r"Сервис:\s*(\S+)", replied.text)
    user_id = int(user_id_match.group(1)) if user_id_match else None
    service = service_match.group(1) if service_match else "unknown"

    if not user_id:
        await message.answer("❌ Не смог определить user_id из чека.")
        return

    # Пытаемся вытащить ID чека из сообщения (или используем msg_id как fallback)
    cheque_id_match = re.search(r"cheque[_-]?id[:\s]*([A-Za-z0-9\-_]+)", message.text or "", re.I)
    cheque_id = cheque_id_match.group(1) if cheque_id_match else str(replied.message_id)

    req_id = await create_check_request(
        user_id=user_id,
        manager_id=message.from_user.id,
        manager_msg_id=message.reply_to_message.message_id,
        user_msg_id=None,
        service=service,
        amount=amount
    )

    await message.answer(f"⏳ Чек на {amount:.2f} ₽ принят. Запущена фоновая проверка.")
    await bot.send_message(user_id,
        f"⏳ Менеджер подтвердил чек на {amount:.2f} ₽. Проверка в фоне.")

    # 🔥 Фоновая проверка
    asyncio.create_task(
        background_verify(bot, req_id, user_id, service, cheque_id, amount)
    )

# ---------- ОБМЕН (без изменений, кроме баланса) ----------
@router.callback_query(F.data == "exchange")
async def exchange_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.amount)
    await cb.message.edit_text(f"Введите сумму в USD (мин. ${MIN_ORDER_USD}):")
    await cb.answer()

@router.message(OrderForm.amount)
async def process_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
    except ValueError:
        await message.answer("❌ Введите число.")
        return
    if amount < MIN_ORDER_USD:
        await message.answer(f"❌ Минимальная сумма — ${MIN_ORDER_USD}.")
        return
    if amount > MAX_ORDER_USD:
        await message.answer(f"❌ Максимум — ${MAX_ORDER_USD}. Проверьте ввод.")
        return
    rate = await get_rate()
    await state.update_data(amount_usd=amount, amount_rub=amount * rate)
    await state.set_state(OrderForm.phone)
    await message.answer(
        f"К обмену: ${amount:.2f} → {amount * rate:.2f} ₽\nВведите номер телефона:")

@router.message(OrderForm.phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await state.set_state(OrderForm.bank)
    await message.answer("Введите название банка:")

@router.message(OrderForm.bank)
async def process_bank(message: Message, state: FSMContext):
    await state.update_data(bank=message.text)
    await state.set_state(OrderForm.full_name)
    await message.answer("Введите ФИО:")

@router.message(OrderForm.full_name)
async def process_full_name(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(full_name=message.text)
    d = await state.get_data()
    order_id = await create_order(message.from_user.id, d["amount_usd"], d["amount_rub"])
    await state.clear()
    await message.answer("✅ Заявка отправлена менеджеру.")
    text = (f"🆕 Заявка #{order_id}\n"
            f"👤 @{message.from_user.username or message.from_user.id}\n"
            f"💵 ${d['amount_usd']:.2f} → {d['amount_rub']:.2f} ₽\n"
            f"📞 {d['phone']}\n🏦 {d['bank']}\n👤 {d['full_name']}")
    for mgr in await get_managers(bot):
        try:
            await bot.send_message(mgr, text, reply_markup=order_actions(order_id))
        except Exception:
            pass

# ---------- АДМИН-ПАНЕЛЬ ----------
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != OWNER_ID and not await is_admin(message.from_user.id):
        return
    await message.answer("Админ-панель:", reply_markup=admin_menu())

@router.callback_query(F.data == "admin_back")
async def admin_back(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID and not await is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("Админ-панель:", reply_markup=admin_menu())
    await cb.answer()

@router.callback_query(F.data == "add_admin")
async def add_admin_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Только владелец.")
        return
    await state.set_state(AdminForm.add_admin)
    await cb.message.edit_text("Введите @username нового админа:")
    await cb.answer()

@router.message(AdminForm.add_admin)
async def add_admin_process(message: Message, state: FSMContext, bot: Bot):
    username = message.text.strip().lstrip("@")
    try:
        chat = await bot.get_chat(f"@{username}")
        await add_admin(chat.id, username)
        await message.answer(f"✅ Админ @{username} (id={chat.id}) добавлен.")
    except Exception:
        await message.answer("❌ Не удалось найти пользователя. Пусть он сначала напишет /start боту.")
    await state.clear()

@router.callback_query(F.data == "set_rate")
async def set_rate_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID and not await is_admin(cb.from_user.id):
        return
    await state.set_state(AdminForm.set_rate)
    await cb.message.edit_text("Введите новый курс (₽ за 1 USD):")
    await cb.answer()

@router.message(AdminForm.set_rate)
async def set_rate_process(message: Message, state: FSMContext):
    try:
        r = float(message.text)
        await set_rate(r)
        await message.answer(f"✅ Курс: {r} ₽/USD")
    except ValueError:
        await message.answer("❌ Введите число.")
    await state.clear()

# ---------- АККАУНТЫ ДЛЯ ЧЕКОВ ----------
@router.callback_query(F.data == "check_accounts")
async def check_acc_menu(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID and not await is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("🏦 Аккаунты для приёма чеков:",
                               reply_markup=check_accounts_menu())
    await cb.answer()

@router.callback_query(F.data.in_(["add_acc_cryptobot", "add_acc_xrocket"]))
async def add_acc_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID and not await is_admin(cb.from_user.id):
        return
    service = "cryptobot" if cb.data.endswith("cryptobot") else "xrocket"
    await state.update_data(acc_service=service)
    await state.set_state(AdminForm.add_check_account)
    await cb.message.edit_text(f"Введите аккаунт для {service} (например @TwaragaCheques):")
    await cb.answer()

@router.message(AdminForm.add_check_account)
async def add_acc_process(message: Message, state: FSMContext):
    d = await state.get_data()
    service = d.get("acc_service", "unknown")
    account = message.text.strip()
    await add_check_account(service, account, message.from_user.id)
    await message.answer(f"✅ Аккаунт {service}: {account} добавлен.",
                         reply_markup=check_accounts_menu())
    await state.clear()

@router.callback_query(F.data == "del_acc_list")
async def del_acc_menu(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID and not await is_admin(cb.from_user.id):
        return
    accs = await list_check_accounts()
    if not accs:
        await cb.message.edit_text("Список пуст.", reply_markup=check_accounts_menu())
    else:
        await cb.message.edit_text("Выберите аккаунт для удаления:", reply_markup=del_acc_list(accs))
    await cb.answer()

@router.callback_query(F.data.startswith("del_acc_"))
async def del_acc(cb: CallbackQuery):
    acc_id = int(cb.data.split("_")[2])
    await delete_check_account(acc_id)
    accs = await list_check_accounts()
    await cb.message.edit_text("✅ Удалено.", reply_markup=del_acc_list(accs))
    await cb.answer()

# ---------- ОБРАБОТКА ЗАЯВКИ ----------
@router.callback_query(F.data.startswith("accept_"))
async def accept_order(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != OWNER_ID and not await is_admin(cb.from_user.id):
        return
    oid = int(cb.data.split("_")[1])
    await update_order_status(oid, "accepted", cb.from_user.id)
    await cb.message.edit_text(f"Заявка #{oid} принята.",
                               reply_markup=order_after_accept(oid))
    await cb.answer()

@router.callback_query(F.data.startswith("give_"))
async def give_amount(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID and not await is_admin(cb.from_user.id):
        return
    oid = int(cb.data.split("_")[1])
    await state.update_data(give_order_id=oid)
    await state.set_state(AdminForm.give_amount)
    await cb.message.edit_text("Введите сумму выдачи (₽):")
    await cb.answer()

@router.message(AdminForm.give_amount)
async def process_give_amount(message: Message, state: FSMContext, bot: Bot):
    try:
        amount = float(message.text)
    except ValueError:
        await message.answer("❌ Введите число.")
        return
    if amount > 1_000_000:
        await message.answer("❌ Слишком большая сумма. Проверьте ввод.")
        return
    d = await state.get_data()
    oid = d["give_order_id"]
    order = await get_order(oid)
    user_id = order[1]
    await update_order_status(oid, "awaiting_payment")
    await bot.send_message(user_id,
        f"💰 Менеджер готов выдать {amount} ₽. Подтвердите получение.",
        reply_markup=payment_status(oid))
    await message.answer(f"⏱ Таймер 15 минут по заявке #{oid} запущен.")
    asyncio.create_task(timer(oid, user_id, bot))
    await state.clear()

async def timer(order_id: int, user_id: int, bot: Bot):
    await asyncio.sleep(900)
    order = await get_order(order_id)
    if order and order[4] == "awaiting_payment":
        await update_order_status(order_id, "expired")
        try:
            await bot.send_message(user_id, "⏰ Время вышло. Заявка отменена.")
        except Exception:
            pass

@router.callback_query(F.data.startswith("paid_"))
async def paid(cb: CallbackQuery):
    oid = int(cb.data.split("_")[1])
    await update_order_status(oid, "paid")
    await cb.message.edit_text("🟢 Клиент подтвердил получение.")
    await cb.answer()

@router.callback_query(F.data.startswith("notpaid_"))
async def notpaid(cb: CallbackQuery, bot: Bot):
    oid = int(cb.data.split("_")[1])
    await update_order_status(oid, "notpaid")
    code = random.randint(1000, 9999)
    order = await get_order(oid)
    user_id = order[1]
    await bot.send_message(user_id,
        f"🔴 Вы указали, что средства не пришли.\n\n"
        f"🎥 Запишите видео с многозадачностью:\n"
        f"• покажите номер и ФИО\n"
        f"• историю платежей (обновите пару раз)\n"
        f"• ваш код на видео: <b>{code}</b>",
        parse_mode="HTML",
        reply_markup=video_verification(oid))
    await cb.answer()
