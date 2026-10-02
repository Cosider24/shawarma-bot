"""Telegram-бот «Шаурма на углу». Запуск: python bot.py"""
import asyncio
import html
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MANAGER_IDS = {int(x) for x in os.getenv("MANAGER_IDS", "").replace(" ", "").split(",") if x.isdigit()}

SHOP_NAME = "Шаурма на углу"
DELIVERY = {"pickup": "Самовывоз", "hall": "Поесть на месте"}

router = Router()


class Checkout(StatesGroup):
    name = State()
    delivery = State()
    confirm = State()


# ---------------- вспомогательные функции ----------------
def is_manager(user_id: int) -> bool:
    return user_id in MANAGER_IDS


def esc(text) -> str:
    return html.escape(str(text))


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    """Заменить текст текущего сообщения (игнорируем «сообщение не изменилось»)."""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer()


def menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("🌯 Каталог", "catalog")],
        [btn("🛒 Корзина", "cart"), btn("📦 Мои заказы", "orders")],
    ]
    if is_manager(user_id):
        rows.append([btn("🧑‍🍳 Новые заказы (менеджер)", "mgr")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


MENU_TEXT = (
    f"<b>🌯 {SHOP_NAME}</b>\n\n"
    "Закажите свежую шаурму прямо в Telegram. Выберите блюда в каталоге, "
    "соберите корзину и оформите заказ. Оплата при получении."
)


def format_items(items) -> str:
    return "\n".join(f"• {esc(i['product_name'])} — {i['qty']} × {i['price']} ₽" for i in items)


def format_order(order, items) -> str:
    return (
        f"<b>Заказ №{order['id']}</b> от {order['created_at']}\n"
        f"Статус: <b>{esc(order['status'])}</b>\n"
        f"Имя: {esc(order['customer_name'])}\n"
        f"Получение: {esc(order['delivery'])}\n"
        f"Расчёт: {esc(order['payment'])}\n\n"
        f"{format_items(items)}\n\n"
        f"<b>Итого: {order['total']} ₽</b>"
    )


# ---------------- старт и меню ----------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    db.register_user(message.from_user.id, message.from_user.full_name)
    await message.answer(MENU_TEXT, reply_markup=menu_kb(message.from_user.id))


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    db.register_user(cb.from_user.id, cb.from_user.full_name)
    await show(cb, MENU_TEXT, menu_kb(cb.from_user.id))


# ---------------- каталог ----------------
@router.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery):
    products = db.get_products()
    if not products:
        await show(cb, "Каталог пока пуст.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ В меню", "menu")]]))
        return
    b = InlineKeyboardBuilder()
    for p in products:
        b.row(btn(f"{p['name']} — {p['price']} ₽", f"p:{p['id']}"))
    b.row(btn("🛒 Корзина", "cart"), btn("⬅️ В меню", "menu"))
    await show(cb, "<b>Каталог</b>\nВыберите позицию:", b.as_markup())


@router.callback_query(F.data.startswith("p:"))
async def cb_product(cb: CallbackQuery):
    product = db.get_product(int(cb.data.split(":")[1]))
    if product is None or not product["available"]:
        await cb.answer("Этой позиции больше нет", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("➕ Добавить в корзину", f"add:{product['id']}")],
            [btn("🛒 Корзина", "cart"), btn("⬅️ К каталогу", "catalog")],
        ]
    )
    text = f"<b>{esc(product['name'])}</b>\n\n{esc(product['description'])}\n\nЦена: <b>{product['price']} ₽</b>"
    await show(cb, text, kb)


@router.callback_query(F.data.startswith("add:"))
async def cb_add(cb: CallbackQuery):
    db.register_user(cb.from_user.id, cb.from_user.full_name)
    product_id = int(cb.data.split(":")[1])
    qty = db.change_qty(cb.from_user.id, product_id, +1)
    if qty == 0:
        await cb.answer("Не удалось добавить: позиции нет в каталоге", show_alert=True)
    else:
        await cb.answer(f"Добавлено. В корзине: {qty} шт.")


# ---------------- корзина ----------------
def render_cart(user_id: int):
    rows = db.get_cart(user_id)
    if not rows:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[btn("🌯 Каталог", "catalog"), btn("⬅️ В меню", "menu")]]
        )
        return "🛒 Ваша корзина пуста.", kb
    lines = [f"• {esc(r['name'])} — {r['qty']} × {r['price']} ₽ = {r['qty'] * r['price']} ₽" for r in rows]
    text = "<b>🛒 Корзина</b>\n\n" + "\n".join(lines) + f"\n\n<b>Итого: {db.cart_total(rows)} ₽</b>"
    b = InlineKeyboardBuilder()
    for r in rows:
        pid = r["product_id"]
        b.row(
            btn("➖", f"dec:{pid}"),
            btn(f"{r['name'][:18]}: {r['qty']}", "noop"),
            btn("➕", f"inc:{pid}"),
            btn("✖️", f"del:{pid}"),
        )
    b.row(btn("✅ Оформить заказ", "checkout"))
    b.row(btn("🌯 Каталог", "catalog"), btn("⬅️ В меню", "menu"))
    return text, b.as_markup()


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "cart")
async def cb_cart(cb: CallbackQuery):
    text, kb = render_cart(cb.from_user.id)
    await show(cb, text, kb)


@router.callback_query(F.data.regexp(r"^(inc|dec|del):\d+$"))
async def cb_cart_change(cb: CallbackQuery):
    action, pid = cb.data.split(":")
    pid = int(pid)
    if action == "inc":
        db.change_qty(cb.from_user.id, pid, +1)
    elif action == "dec":
        db.change_qty(cb.from_user.id, pid, -1)
    else:
        db.remove_item(cb.from_user.id, pid)
    text, kb = render_cart(cb.from_user.id)
    await show(cb, text, kb)


# ---------------- оформление заказа ----------------
@router.callback_query(F.data == "checkout")
async def cb_checkout(cb: CallbackQuery, state: FSMContext):
    if not db.get_cart(cb.from_user.id):
        await cb.answer("Корзина пуста — оформлять нечего", show_alert=True)
        return
    await state.clear()
    await state.set_state(Checkout.name)
    await cb.message.answer("Как к вам обращаться? Напишите имя или псевдоним (до 50 символов).")
    await cb.answer()


@router.message(Checkout.name)
async def got_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 50 or name.startswith("/"):
        await message.answer("Пожалуйста, отправьте имя обычным текстом (от 1 до 50 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(Checkout.delivery)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(DELIVERY["pickup"], "dl:pickup"), btn(DELIVERY["hall"], "dl:hall")],
            [btn("❌ Отмена", "menu")],
        ]
    )
    await message.answer("Выберите способ получения:", reply_markup=kb)


@router.callback_query(Checkout.delivery, F.data.startswith("dl:"))
async def got_delivery(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key not in DELIVERY:
        await cb.answer("Неизвестный способ", show_alert=True)
        return
    rows = db.get_cart(cb.from_user.id)
    if not rows:
        await state.clear()
        await show(cb, "Корзина пуста.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ В меню", "menu")]]))
        return
    await state.update_data(delivery=DELIVERY[key])
    await state.set_state(Checkout.confirm)
    data = await state.get_data()
    lines = [f"• {esc(r['name'])} — {r['qty']} × {r['price']} ₽" for r in rows]
    text = (
        "<b>Проверьте заказ</b>\n\n" + "\n".join(lines) +
        f"\n\n<b>Итого: {db.cart_total(rows)} ₽</b>\n"
        f"Имя: {esc(data['name'])}\nПолучение: {esc(DELIVERY[key])}\nРасчёт: при получении"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Подтвердить заказ", "confirm")],
            [btn("🛒 Вернуться в корзину", "cart"), btn("❌ Отмена", "menu")],
        ]
    )
    await show(cb, text, kb)


@router.callback_query(F.data == "confirm")
async def cb_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("name") or not data.get("delivery"):
        # состояние уже очищено — значит, заказ был оформлен или оформление отменено
        await cb.answer("Этот заказ уже оформлен или оформление не начато.", show_alert=True)
        return
    order_id = db.create_order(cb.from_user.id, data["name"], data["delivery"])
    await state.clear()
    if order_id is None:
        await cb.answer("Корзина пуста — заказ не создан.", show_alert=True)
        return
    order, items = db.get_order(order_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[btn("📦 Мои заказы", "orders"), btn("⬅️ В меню", "menu")]]
    )
    await show(cb, "✅ <b>Заказ оформлен!</b>\n\n" + format_order(order, items), kb)
    for manager_id in MANAGER_IDS:  # уведомляем менеджеров
        try:
            await bot.send_message(manager_id, f"🔔 Новый заказ №{order_id} на {order['total']} ₽")
        except Exception:
            logging.warning("Не удалось уведомить менеджера %s", manager_id)


# ---------------- мои заказы ----------------
@router.callback_query(F.data == "orders")
async def cb_orders(cb: CallbackQuery):
    orders = db.get_user_orders(cb.from_user.id)
    b = InlineKeyboardBuilder()
    for o in orders:
        b.row(btn(f"№{o['id']} • {o['total']} ₽ • {o['status']}", f"o:{o['id']}"))
    b.row(btn("⬅️ В меню", "menu"))
    text = "<b>📦 Мои заказы</b>" if orders else "У вас пока нет заказов."
    await show(cb, text, b.as_markup())


@router.callback_query(F.data.startswith("o:"))
async def cb_order(cb: CallbackQuery):
    order, items = db.get_order(int(cb.data.split(":")[1]))
    # покупатель видит только свои заказы
    if order is None or order["user_id"] != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ К заказам", "orders")]])
    await show(cb, format_order(order, items), kb)


# ---------------- менеджер ----------------
@router.callback_query(F.data == "mgr")
async def cb_mgr(cb: CallbackQuery):
    if not is_manager(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    orders = db.get_new_orders()
    b = InlineKeyboardBuilder()
    for o in orders:
        b.row(btn(f"№{o['id']} • {o['total']} ₽ • {o['customer_name']}", f"mo:{o['id']}"))
    b.row(btn("⬅️ В меню", "menu"))
    await show(cb, "<b>Новые заказы</b>" if orders else "Новых заказов нет.", b.as_markup())


@router.callback_query(F.data.startswith("mo:"))
async def cb_mgr_order(cb: CallbackQuery):
    if not is_manager(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    order_id = int(cb.data.split(":")[1])
    order, items = db.get_order(order_id)
    if order is None:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for idx, status in enumerate(db.STATUSES):
        b.row(btn(f"→ {status}", f"st:{order_id}:{idx}"))
    b.row(btn("⬅️ К новым заказам", "mgr"))
    await show(cb, format_order(order, items), b.as_markup())


@router.callback_query(F.data.regexp(r"^st:\d+:\d+$"))
async def cb_set_status(cb: CallbackQuery, bot: Bot):
    if not is_manager(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, order_id, idx = cb.data.split(":")
    order_id, idx = int(order_id), int(idx)
    if idx >= len(db.STATUSES):
        await cb.answer("Неизвестный статус", show_alert=True)
        return
    order, _items = db.get_order(order_id)
    if order is None:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    db.set_status(order_id, db.STATUSES[idx])
    order, items = db.get_order(order_id)
    b = InlineKeyboardBuilder()
    for i, status in enumerate(db.STATUSES):
        b.row(btn(f"→ {status}", f"st:{order_id}:{i}"))
    b.row(btn("⬅️ К новым заказам", "mgr"))
    await show(cb, format_order(order, items), b.as_markup())
    try:  # сообщаем покупателю
        await bot.send_message(order["user_id"], f"Статус вашего заказа №{order_id}: <b>{esc(order['status'])}</b>")
    except Exception:
        logging.warning("Не удалось уведомить покупателя %s", order["user_id"])


# ---------------- запуск ----------------
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN. Создайте файл .env по образцу .env.example")
    db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
