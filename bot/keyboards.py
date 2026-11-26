from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💱 Обменять", callback_data="exchange")],
        [InlineKeyboardButton(text="💳 Пополнить чеком", callback_data="deposit")],
        [InlineKeyboardButton(text="📊 Курс", callback_data="rate")],
        [InlineKeyboardButton(text="💼 Баланс", callback_data="balance")],
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="add_admin")],
        [InlineKeyboardButton(text="⚙️ Установить курс", callback_data="set_rate")],
        [InlineKeyboardButton(text="🏦 Аккаунты для чеков", callback_data="check_accounts")],
    ])

def check_accounts_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ CryptoBot", callback_data="add_acc_cryptobot"),
         InlineKeyboardButton(text="➕ xRocket", callback_data="add_acc_xrocket")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="del_acc_list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")],
    ])

def del_acc_list(accounts):
    rows = []
    for acc_id, service, account in accounts:
        rows.append([InlineKeyboardButton(
            text=f"❌ {service}: {account}", callback_data=f"del_acc_{acc_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="check_accounts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def order_actions(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{order_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}")]
    ])

def order_after_accept(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Дать сумму", callback_data=f"give_{order_id}")],
        [InlineKeyboardButton(text="🏁 Завершить", callback_data=f"finish_{order_id}")]
    ])

def payment_status(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Пришло", callback_data=f"paid_{order_id}"),
         InlineKeyboardButton(text="🔴 Не пришло", callback_data=f"notpaid_{order_id}")]
    ])

def video_verification(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📹 Отправить видео", callback_data=f"video_{order_id}")]
    ])
