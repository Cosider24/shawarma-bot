"""HTTP-сервер: раздаёт Mini App и API кабинета менеджера. Работает в одном процессе с ботом."""
import hashlib
import hmac
import json
import logging
import time
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import config
import finance
import notify
import yookassa
import asyncio
import db
import stock
import worker

WEBAPP_DIR = Path(__file__).parent / "webapp"
money = finance.fmt_money


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, token: str, max_age: int, now=None) -> dict:
    """Проверка Telegram.WebApp.initData по правилам Telegram (HMAC-SHA256 + срок auth_date)."""
    if not init_data or not token:
        raise AuthError("Нет данных авторизации Telegram")
    try:
        parsed = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        pairs = dict(parsed)
        if len(pairs)!=len(parsed):
            raise ValueError('duplicate initData field')
    except ValueError:
        raise AuthError("Неверные данные авторизации")
    got_hash = pairs.pop("hash", None)
    if not got_hash:
        raise AuthError("Неверные данные авторизации")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got_hash):
        raise AuthError("Подпись данных Telegram не прошла проверку")
    try:
        age = (now if now is not None else time.time()) - int(pairs["auth_date"])
        user = json.loads(pairs["user"])
        user_id = int(user["id"])
        if user_id<=0 or not isinstance(user.get('first_name',''),str):
            raise ValueError('invalid user')
    except (KeyError, ValueError, TypeError):
        raise AuthError("Неверные данные авторизации")
    if age > max_age or age < -60:
        raise AuthError("Данные авторизации устарели. Закройте и откройте кабинет заново.")
    return {"id": user_id, "name": user.get("first_name", "")}


def jerr(message, status=400):
    return web.json_response({"error": message}, status=status)


@web.middleware
async def guard(request, handler):
    if request.path.startswith("/api/"):
        header = request.headers.get("Authorization", "")
        if not header.startswith("tma "):
            return jerr("Откройте кабинет из Telegram-бота", 401)
        try:  # токен берётся ТОЛЬКО из конфигурации сервера
            user = validate_init_data(header[4:], config.BOT_TOKEN, config.INITDATA_MAX_AGE)
        except AuthError as e:
            return jerr(str(e), 401)
        if not request.path.startswith('/api/shop/') and user["id"] not in config.MANAGER_IDS:
            return jerr("Доступ только для менеджера", 403)
        request["user"] = user
    try:
        if request.path.startswith('/api/'):
            await asyncio.to_thread(db.register_user,request['user']['id'],request['user']['name'])
        return await handler(request)
    except finance.FinanceError as e:
        return jerr(str(e), e.status)
    except (ValueError,TypeError):
        return jerr('Проверьте формат данных',400)
    except sqlite3.IntegrityError:
        return jerr('Конфликт операции. Обновите данные и проверьте прежний результат.',409)
    except sqlite3.OperationalError as e:
        if 'locked' in str(e) or 'busy' in str(e):
            return jerr('Склад занят. Повторите с тем же ключом запроса.',503)
        raise
    except web.HTTPException:
        raise
    except Exception:
        logging.exception("Ошибка API")
        return jerr("Ошибка сервера. Проверьте результат и безопасно повторите с тем же ключом.", 500)


def _period(request):
    today = date.today()
    a = request.query.get("from") or today.replace(day=1).isoformat()
    b = request.query.get("to") or today.isoformat()
    finance.period_bounds(a, b)
    return a, b


async def _body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        raise finance.FinanceError("Неверный формат запроса")


def _id(request, key="id"):
    try:
        return int(request.match_info[key])
    except ValueError:
        raise finance.FinanceError("Неверный номер", 404)


# ------------------------------------------------------------------ API
async def api_me(request):
    return web.json_response({
        "id": request["user"]["id"], "name": request["user"]["name"],
        "demo": config.PAYMENT_MODE != "yookassa", "payment_mode": config.PAYMENT_MODE,
        "categories": finance.EXPENSE_CATEGORIES, "today": date.today().isoformat(),
    })


async def api_summary(request):
    return web.json_response(await asyncio.to_thread(finance.summary,*_period(request)))


async def api_journal(request):
    return web.json_response({"items": finance.journal(*_period(request))})


async def api_orders(request):
    return web.json_response({"items": finance.list_orders()})


async def api_order(request):
    return web.json_response(await asyncio.to_thread(finance.order_card,_id(request)))


async def api_add_expense(request):
    b = await _body(request)
    r = finance.add_expense(request["user"]["id"], b.get("category"), b.get("amount"), b.get("date"),
                            b.get("description"), str(b.get("request_id") or "")[:64] or None)
    return web.json_response(r)


async def api_void_expense(request):
    finance.void_expense(_id(request), request["user"]["id"])
    return web.json_response({"ok": True})


async def api_edit_expense(request):
    b = await _body(request)
    r = finance.edit_expense(_id(request), request["user"]["id"], b.get("category"), b.get("amount"),
                             b.get("date"), b.get("description"))
    return web.json_response(r)


async def api_refund(request):
    await asyncio.to_thread(stock.request_refund,_id(request))
    return web.json_response({'status':'refund_pending','message':'Задание сохранено. Результат проверит фоновый работник.'})


async def api_refund_finish(request):
    await worker.process_refunds()
    return web.json_response({'status':'Проверка выполнена; обновите карточку'})


async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def shop_index(request):
    return web.FileResponse(WEBAPP_DIR / 'shop.html',headers={'Cache-Control':'no-store'})


async def shop_me(request):
    return web.json_response(dict(request['user'],manager=request['user']['id'] in config.MANAGER_IDS,
                                  simulator=config.PAYMENT_MODE=='simulator',reserve_seconds=config.RESERVATION_SECONDS))


async def shop_catalog(request):
    return web.json_response({'items':await asyncio.to_thread(stock.catalog)})


async def shop_checkout(request):
    b=await _body(request)
    if set(b)-{'items','name','delivery','request_key','note'}:
        raise stock.ShopError('Сумму и срок определяет сервер')
    result=await asyncio.to_thread(stock.checkout,request['user']['id'],b.get('items'),b.get('name'),b.get('delivery'),b.get('request_key'),b.get('note',''))
    return web.json_response(result)


async def shop_orders(request):
    return web.json_response({'items':[dict(o) for o in await asyncio.to_thread(db.get_user_orders,request['user']['id'])]})


async def shop_order(request):
    return web.json_response(await asyncio.to_thread(stock.customer_order,_id(request),request['user']['id']))


async def shop_cancel(request):
    return web.json_response(await asyncio.to_thread(stock.cancel,_id(request),request['user']['id']))


async def shop_pay(request):
    oid=_id(request); uid=request['user']['id']
    provider='yookassa' if config.PAYMENT_MODE=='yookassa' else 'simulator'
    p=await asyncio.to_thread(stock.create_payment,oid,uid,provider)
    if provider=='simulator':
        return web.json_response(dict(p,simulator=True))
    bot=request.app.get('bot')
    if not bot:
        raise stock.ShopError('Оплата доступна через запущенного Telegram-бота',503)
    from aiogram.types import LabeledPrice
    link=await bot.create_invoice_link(title=f'Заказ №{oid}',description='Шаурма на углу. Учебная оплата.',payload=f'pay:{p["id"]}',
                                       provider_token=config.PROVIDER_TOKEN,currency='RUB',prices=[LabeledPrice(label=f'Заказ №{oid}',amount=p['amount_kop'])])
    return web.json_response(dict(p,invoice_url=link))


async def shop_simulate(request):
    if config.PAYMENT_MODE!='simulator':
        raise stock.ShopError('Имитатор отключён',404)
    pid=_id(request); p=finance.get_payment(pid)
    if not p or p['buyer_id']!=request['user']['id'] or p['provider']!='simulator':
        raise stock.ShopError('Платёж не найден',404)
    b=await _body(request)
    if set(b)-{'result'} or b.get('result') not in ('succeeded','canceled'):
        raise stock.ShopError('Имитатор принимает только результат своего платежа')
    if b['result']=='canceled':
        return web.json_response({'changed':await asyncio.to_thread(finance.cancel_payment,pid)})
    result=await asyncio.to_thread(stock.payment_success,pid,p['amount_kop'],'RUB',f'sim-{pid}')
    if config.TEST_MODE and __import__('os').getenv('SHOP_FAILPOINT')=='after_late_payment' and result.get('conflict'):
        __import__('os')._exit(87)
    return web.json_response(result)


async def api_stock(request):
    return web.json_response(await asyncio.to_thread(stock.dashboard))


async def api_receive(request):
    b=await _body(request)
    if set(b)-{'product_id','qty','request_key'}:
        raise stock.ShopError('Недопустимые поля')
    return web.json_response(await asyncio.to_thread(stock.receive,b.get('product_id'),b.get('qty'),request['user']['id'],b.get('request_key')))


async def api_sweep(request):
    await worker.tick()
    return web.json_response({'ok':True})


async def api_cancel_order(request):
    return web.json_response(await asyncio.to_thread(stock.cancel,_id(request),request['user']['id'],True))


async def api_status(request):
    b=await _body(request)
    await asyncio.to_thread(db.set_status,_id(request),b.get('status'))
    return web.json_response({'ok':True})


def create_app(bot=None):
    app = web.Application(middlewares=[guard])
    app["bot"] = bot
    app.cleanup_ctx.append(worker.lifecycle)
    app.add_routes([
        web.get("/", index),
        web.get('/shop',shop_index),
        web.static('/assets',WEBAPP_DIR / 'assets'),
        web.get('/api/shop/me',shop_me),
        web.get('/api/shop/catalog',shop_catalog),
        web.post('/api/shop/checkout',shop_checkout),
        web.get('/api/shop/orders',shop_orders),
        web.get('/api/shop/orders/{id}',shop_order),
        web.post('/api/shop/orders/{id}/cancel',shop_cancel),
        web.post('/api/shop/orders/{id}/pay',shop_pay),
        web.post('/api/shop/payments/{id}/simulate',shop_simulate),
        web.get('/api/stock',api_stock),
        web.post('/api/stock/receive',api_receive),
        web.post('/api/stock/sweep',api_sweep),
        web.post('/api/orders/{id}/cancel',api_cancel_order),
        web.post('/api/orders/{id}/status',api_status),
        web.get("/api/me", api_me),
        web.get("/api/summary", api_summary),
        web.get("/api/journal", api_journal),
        web.get("/api/orders", api_orders),
        web.get("/api/orders/{id}", api_order),
        web.post("/api/orders/{id}/refund", api_refund),
        web.post("/api/orders/{id}/refund/finish", api_refund_finish),
        web.post("/api/expenses", api_add_expense),
        web.post("/api/expenses/{id}/void", api_void_expense),
        web.post("/api/expenses/{id}/edit", api_edit_expense),
    ])
    return app


if __name__=='__main__':
    db.init_db()
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(),host=config.WEB_HOST,port=config.WEB_PORT)
