"""Telegram-бот «Шаурма на углу». Запуск: python bot.py"""
import asyncio
import html
import logging
import os
import sys
from datetime import date

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message,
    PreCheckoutQuery, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

import config
import db
import finance
import notify
import server
import bot_store
import bot_recipe
import panels
import stock
import uuid
from pathlib import Path
from aiogram.types import FSInputFile

BOT_TOKEN = config.BOT_TOKEN
MANAGER_IDS = config.MANAGER_IDS
money = finance.fmt_money

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


async def show(cb, text, kb, image_id=1):
    await panels.draw(cb.bot,cb.from_user.id,cb.message.chat.id,text,kb,image_id,cb.message)
    await cb.answer()


def menu_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("🌯 Каталог", "catalog")],
        [btn("🛒 Корзина", "cart"), btn("📦 Мои заказы", "orders")],
    ]
    if config.WEBAPP_URL.startswith('https://'):
        rows.insert(0,[InlineKeyboardButton(text='🌯 Собрать шаурму · магазин',web_app=WebAppInfo(url=config.WEBAPP_URL+'/shop'))])
    if is_manager(user_id):
        if config.WEBAPP_URL.startswith("https://"):
            rows.append([InlineKeyboardButton(text="📊 Финансовый кабинет",
                                              web_app=WebAppInfo(url=config.WEBAPP_URL))])
        rows.append([btn("🧑‍🍳 Новые заказы (менеджер)", "mgr")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


MENU_TEXT = (
    f"<b>🌯 {SHOP_NAME}</b>\n\n"
    "Закажите свежую шаурму прямо в Telegram. Выберите блюда в каталоге, "
    "настройте добавки и состав, оформите заказ и оплатите его онлайн (учебная оплата)."
)


def format_items(items) -> str:
    return "\n".join(f"• {esc(i['product_name'])} — {i['qty']} × {i['price']} ₽" for i in items)


def format_order(order, items) -> str:
    c=db.get_conn()
    try:
        r=c.execute('SELECT * FROM reservations WHERE order_id=?',(order['id'],)).fetchone()
    finally:
        c.close()
    extra=''
    if r and r['state']=='active':
        extra=f"\nРезерв: ещё {max(0,int(r['expires_at']-stock.now()))} сек. Поздняя оплата вернётся автоматически."
    return (
        f"<b>Заказ №{order['id']}</b> от {order['created_at']}\n"
        f"Статус: <b>{esc(order['status'])}</b>\n"
        f"Оплата: <b>{esc(finance.PAY_LABELS.get(order['pay_status'], order['pay_status']))}</b>\n"
        f"Имя: {esc(order['customer_name'])}\n"
        f"Получение: {esc(order['delivery'])}\n"
        f"{format_items(items)}\n\n"
        f"<b>Итого: {order['total']} ₽</b>{extra}"
    )


# ---------------- старт и меню ----------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    db.register_user(message.from_user.id, message.from_user.full_name)
    await panels.message(message,MENU_TEXT,menu_kb(message.from_user.id))


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
    products = stock.catalog()
    if not products:
        await show(cb, "Каталог пока пуст.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ В меню", "menu")]]))
        return
    b = InlineKeyboardBuilder()
    for p in products:
        b.row(btn(f"{p['name']} — {p['price']} ₽ · {p['stock_available']} шт.", f"p:{p['id']}"))
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
    await show(cb,text,kb,product['id'])


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
    rows = bot_store.cart(user_id)
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
    if not bot_store.cart(cb.from_user.id):
        await cb.answer("Корзина пуста — оформлять нечего", show_alert=True)
        return
    await state.clear()
    await state.set_state(Checkout.name)
    await show(cb,"Как к вам обращаться? Напишите имя или псевдоним (до 50 символов).", InlineKeyboardMarkup(inline_keyboard=[[btn("Отмена","menu")]]))


@router.message(Checkout.name)
async def got_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 50 or name.startswith("/"):
        await panels.message(message,"Пожалуйста, отправьте имя обычным текстом (от 1 до 50 символов).",InlineKeyboardMarkup(inline_keyboard=[[btn("Отмена","menu")]]))
        return
    await state.update_data(name=name)
    await state.set_state(Checkout.delivery)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(DELIVERY["pickup"], "dl:pickup"), btn(DELIVERY["hall"], "dl:hall")],
            [btn("❌ Отмена", "menu")],
        ]
    )
    await panels.message(message,"Выберите способ получения:",kb)


@router.callback_query(Checkout.delivery, F.data.startswith("dl:"))
async def got_delivery(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key not in DELIVERY:
        await cb.answer("Неизвестный способ", show_alert=True)
        return
    rows = bot_store.cart(cb.from_user.id)
    if not rows:
        await state.clear()
        await show(cb, "Корзина пуста.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ В меню", "menu")]]))
        return
    await state.update_data(delivery=DELIVERY[key],request_key=str(uuid.uuid4()),items=[r['raw'] for r in rows],cart_snapshot=[{k:r[k] for k in ('id','product_id','qty','options_json')} for r in rows])
    await state.set_state(Checkout.confirm)
    data = await state.get_data()
    lines = [f"• {esc(r['name'])} — {r['qty']} × {r['price']} ₽" for r in rows]
    text = (
        "<b>Проверьте заказ</b>\n\n" + "\n".join(lines) +
        f"\n\n<b>Итого: {db.cart_total(rows)} ₽</b>\n"
        f"Имя: {esc(data['name'])}\nПолучение: {esc(DELIVERY[key])}\nОплата: онлайн после подтверждения"
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
    try:
        result = await asyncio.to_thread(stock.checkout,cb.from_user.id,data['items'],data['name'],data['delivery'],data['request_key'],bot_cart_snapshot=data['cart_snapshot'])
        order_id=result['id']
    except finance.FinanceError as e:
        await cb.answer(str(e),show_alert=True)
        return
    await state.clear()
    if order_id is None:
        await cb.answer("Корзина пуста — заказ не создан.", show_alert=True)
        return
    order, items = db.get_order(order_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("💳 Оплатить", f"pay:{order_id}"),btn("Отменить",f"cancel:{order_id}")],
        [btn("📦 Мои заказы", "orders"), btn("⬅️ В меню", "menu")],
    ])
    await show(cb, "✅ <b>Заказ оформлен!</b> Ожидает оплаты.\n\n" + format_order(order, items), kb)
    await notify.managers(bot, f"🔔 Новый заказ №{order_id} на {order['total']} ₽ (ожидает оплаты)",exclude={cb.from_user.id})


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
    rows = []
    if order["pay_status"] == "awaiting_payment" and order["status"] != "Отменён":
        rows.append([btn("💳 Оплатить", f"pay:{order['id']}"),btn("Отменить",f"cancel:{order['id']}")])
    rows.append([btn("⬅️ К заказам", "orders")])
    await show(cb, format_order(order, items), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r'^cancel:\d+$'))
async def cb_cancel_order(cb: CallbackQuery):
    try:
        await asyncio.to_thread(stock.cancel,int(cb.data.split(':')[1]),cb.from_user.id)
        await cb.answer('Заказ отменён; резерв освобождён.',show_alert=True)
    except finance.FinanceError as e:
        await cb.answer(str(e),show_alert=True)


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
    try:
        await asyncio.to_thread(db.set_status,order_id,db.STATUSES[idx])
    except finance.FinanceError as e:
        await cb.answer(str(e),show_alert=True)
        return
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


# ---------------- оплата ----------------
@router.callback_query(F.data.regexp(r"^pay:\d+$"))
async def cb_pay(cb: CallbackQuery, bot: Bot):
    order_id = int(cb.data.split(":")[1])
    provider = "yookassa" if config.PAYMENT_MODE == "yookassa" else "simulator"
    try:  # сумму сервер берёт из сохранённого заказа
        p = finance.create_payment(order_id, cb.from_user.id, provider)
    except finance.FinanceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    if provider == "yookassa":
        _order, items = db.get_order(order_id)
        prices = [LabeledPrice(label=i["product_name"][:60], amount=i["price"] * i["qty"] * 100) for i in items]
        if sum(x.amount for x in prices) != p["amount_kop"]:
            prices = [LabeledPrice(label=f"Заказ №{order_id}", amount=p["amount_kop"])]
        link=await bot.create_invoice_link(title=f'Заказ №{order_id}',description='Шаурма на углу · тестовая оплата',payload=f"pay:{p['id']}",provider_token=config.PROVIDER_TOKEN,currency=p['currency'],prices=prices)
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть учебную оплату',url=link)],[btn('К заказу',f'o:{order_id}')]])
        await show(cb,f"<b>Оплата заказа №{order_id}</b>\n\n{money(p['amount_kop'])} · тестовая ЮKassa\nКнопка откроет платёжное окно Telegram. Резерв продолжает отсчитываться.",kb)

    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            btn("✅ Оплата прошла", f"simok:{p['id']}"), btn("❌ Отменить платёж", f"simno:{p['id']}")]])
        kb.inline_keyboard.append([btn('К заказу',f'o:{order_id}')])
        await show(cb,f"<b>Учебная оплата заказа №{order_id}</b>\n\n{money(p['amount_kop'])}\nРеальные деньги не списываются.",kb)

    await cb.answer()


async def _payment_succeeded(bot, payment_id, amount_kop, currency, provider_id, tg_id, user_id):
    try:
        r = finance.apply_payment_success(payment_id, amount_kop, currency, provider_id, tg_id)
    except finance.FinanceError as e:
        logging.error("Платёж %s не принят: %s", payment_id, e)
        await notify.managers(bot, f"⚠️ Платёж №{payment_id} требует ручной проверки: {e}")
        return "error", str(e)
    if r["duplicate"]:
        return "duplicate", None
    if r.get('conflict'):

        await notify.managers(bot,f"Поздняя оплата заказа №{r['order_id']}: задание возврата сохранено.",exclude={user_id})
        return 'conflict', None

    await notify.managers(bot, f"💰 Оплачен заказ №{r['order_id']}: {money(r['amount_kop'])}",exclude={user_id})
    return "ok", None


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    err = finance.check_precheckout(q.invoice_payload, q.total_amount, q.currency, q.from_user.id)
    await q.answer(ok=err is None, error_message=err)


@router.message(F.successful_payment)
async def got_payment(message: Message, bot: Bot):
    sp = message.successful_payment
    try:
        pid = finance.parse_payload(sp.invoice_payload)
    except finance.FinanceError:
        return
    p=finance.get_payment(pid)
    if p is None or p['buyer_id']!=message.from_user.id or p['provider']!='yookassa':
        logging.error('Успешная оплата с несоответствующим владельцем/провайдером')
        return
    await _payment_succeeded(bot, pid, sp.total_amount, sp.currency,
                             sp.provider_payment_charge_id, sp.telegram_payment_charge_id, message.from_user.id)
    order,items=db.get_order(p['order_id'])
    await panels.message(message,format_order(order,items),InlineKeyboardMarkup(inline_keyboard=[[btn('Мои заказы','orders'),btn('Меню','menu')]]))


@router.callback_query(F.data.regexp(r"^sim(ok|no):\d+$"))
async def cb_simulator(cb: CallbackQuery, bot: Bot):
    if config.PAYMENT_MODE!='simulator':
        await cb.answer('Учебный имитатор отключён',show_alert=True)
        return
    action, pid = cb.data.split(":")
    pid = int(pid)
    p = finance.get_payment(pid)
    if p is None or p["buyer_id"] != cb.from_user.id or p["provider"] != "simulator":
        await cb.answer("Платёж не найден", show_alert=True)
        return
    if action == "simno":
        done = finance.cancel_payment(pid)
        await cb.answer("Платёж отменён, поступления нет." if done else "Платёж уже обработан.", show_alert=True)
    else:  # те же проверки суммы/заказа/повтора, что и у ЮKassa
        status, err = await _payment_succeeded(bot, pid, p["amount_kop"], p["currency"],
                                               f"sim-{pid}", None, cb.from_user.id)
        await cb.answer({"conflict":"Резерв закончился, оформляется возврат.","ok": "Оплата прошла.", "duplicate": "Платёж уже проведён — повтор не учтён."}
                        .get(status, err or "Ошибка"), show_alert=True)
    order,items=db.get_order(p['order_id'])
    await show(cb,format_order(order,items),InlineKeyboardMarkup(inline_keyboard=[[btn('Обновить',f"o:{order['id']}")],[btn('Мои заказы','orders'),btn('Меню','menu')]]))


@router.message(Command("summary"))
async def cmd_summary(message: Message):
    if not is_manager(message.from_user.id):  # роль проверяет сервер, не текст команды
        await message.answer("Команда доступна только менеджеру.")
        return
    today = date.today().isoformat()
    lines = []
    for title, a in (("Сегодня", today), ("С начала месяца", date.today().replace(day=1).isoformat()),
                     ("За всё время", "2000-01-01")):
        r = finance.summary(a, today)
        lines.append(f"<b>{title}</b>\nПоступления: {money(r['receipts'])}  Возвраты: {money(r['refunds'])}\n"
                     f"Расходы: {money(r['expenses'])}\nДенежный поток: <b>{money(r['cash_flow'])}</b>\n"
                     f"Учебный результат: <b>{money(r['result'])}</b>")
    await message.answer("📊 " + "\n\n".join(lines), reply_markup=notify.webapp_kb())


# ---------------- запуск ----------------
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN. Создайте файл .env по образцу .env.example")
    db.init_db()
    bot_store.init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    guard=bot_recipe.SerialUser()
    dp.callback_query.outer_middleware(guard)
    dp.message.outer_middleware(guard)
    dp.include_router(bot_recipe.router)
    dp.include_router(router)
    if config.PAYMENT_MODE == "yookassa" and not config.PROVIDER_TOKEN:
        sys.exit("PAYMENT_MODE=yookassa, но PROVIDER_TOKEN не задан в .env")
    runner = web.AppRunner(server.create_app(bot))  # Mini App + API в том же процессе
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    logging.info("Mini App: http://%s:%s  режим оплаты: %s", config.WEB_HOST, config.WEB_PORT, config.PAYMENT_MODE)
    if config.WEBAPP_URL.startswith('https://'):
        from aiogram.types import MenuButtonWebApp
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text='Магазин',web_app=WebAppInfo(url=config.WEBAPP_URL+'/shop')))
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    if os.getenv('SHOP_MANAGED')=='1' or config.TEST_MODE:
        asyncio.run(main())
    else:
        import run
        run.main()
