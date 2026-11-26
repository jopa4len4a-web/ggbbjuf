from aiogram.fsm.state import State, StatesGroup

class OrderForm(StatesGroup):
    amount = State()
    phone = State()
    bank = State()
    full_name = State()

class AdminForm(StatesGroup):
    add_admin = State()
    set_rate = State()
    give_amount = State()
    add_check_account = State()

class DepositForm(StatesGroup):
    waiting_cheque = State()   # ждём чек от юзера
