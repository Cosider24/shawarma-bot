"""Конструктор и варианты корзины прямо в Telegram, без открытия Mini App."""
import asyncio
import html
import math
from collections import defaultdict
from aiogram import BaseMiddleware,F,Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton,InlineKeyboardMarkup
import bot_store as store
import db
import finance
import panels
import stock

router=Router()
esc=lambda value:html.escape(str(value))
def btn(text,data):return InlineKeyboardButton(text=text,callback_data=data)
def kb(rows):return InlineKeyboardMarkup(inline_keyboard=rows)


class SerialUser(BaseMiddleware):
    def __init__(self):self.locks=defaultdict(asyncio.Lock)
    async def __call__(self,handler,event,data):
        user=getattr(event,'from_user',None)
        if user is None:return await handler(event,data)
        message=getattr(event,'message',None) or event
        if getattr(message,'chat',None) and message.chat.type!='private':return
        async with self.locks[user.id]:
            await asyncio.to_thread(db.register_user,user.id,user.full_name)
            try:return await handler(event,data)
            except finance.FinanceError as error:
                if hasattr(event,'data'):await event.answer(str(error),show_alert=True)
                else:await panels.message(event,esc(str(error)),kb([[btn('Корзина','cart'),btn('Меню','menu')]]))


async def show(cb,text,rows,pid=1):
    await panels.draw(cb.bot,cb.from_user.id,cb.message.chat.id,text,kb(rows),pid,cb.message)
    await cb.answer()


async def product(cb,r):
    item=store.recipe_price(r);pid=r['product_id'];meta=stock.product_meta(pid);token=r['token']
    p=next((p for p in stock.catalog() if p['id']==pid),None)
    if p is None:raise stock.ShopError('Товар недоступен',404)
    rows=[]
    for key,a in meta['additions'].items():
        rows.append([btn(f'{"✅" if key in r["add"] else "＋"} {a["name"]} · +{a["price"]} ₽',f'ra:{token}:{key}')])
    for index,name in enumerate(meta['ingredients']):
        rows.append([btn(f'{"✅" if name in r["remove"] else "▫️"} Без {stock.WITHOUT.get(name,name)}',f'rr:{token}:{index}')])
    rows.append([btn('−',f'rq:{token}:-1'),btn(f'{r["qty"]} шт.','noop'),btn('+',f'rq:{token}:1')])
    if p['stock_available']:rows.append([btn(f'🛒 В корзину · {item["price"]*r["qty"]} ₽',f'radd:{token}')])
    rows.append([btn('‹ Каталог','catalog'),btn('Корзина','cart')])
    selected=[stock.ADDITIONS[k]['name'] for k in sorted(r['add'])]+['без '+stock.WITHOUT.get(k,k) for k in sorted(r['remove'])]
    text=f'<b>{esc(p["name"])}</b>\n{esc(p["description"])}\n\n'
    text+=f'Твой рецепт: {esc(", ".join(selected) if selected else "стандартный состав")}\n'
    text+=f'<b>{item["price"]} ₽ за порцию · {r["qty"]} шт.</b>\nДоступно: {p["stock_available"]} шт.'
    text+='\n\nОтметь добавки и что убрать. Снятие ингредиента не уменьшает цену.' if meta['ingredients'] else '\n\nВыбери количество.'
    if not p['stock_available']:text+='\n<b>Нет в наличии.</b>'
    await show(cb,text,rows,pid)


async def basket(cb,page=0,note=''):
    items=store.cart(cb.from_user.id);rows=[]
    if not items:return await show(cb,'<b>Корзина пока пуста</b>\n\nВыбери блюдо и собери свой рецепт.',[[btn('🌯 Каталог','catalog'),btn('Меню','menu')]])
    page=max(0,min(page,(len(items)-1)//3));lines=['<b>🛒 Твоя корзина</b>']
    if note:lines.append(esc(note))
    for index,i in enumerate(items[page*3:page*3+3],start=page*3+1):
        title=i['product_name'];title=title if len(title)<115 else title[:112]+'…'
        lines.append(f'{index}. {esc(title)}\n{i["qty"]} × {i["price"]} ₽ = <b>{i["qty"]*i["price"]} ₽</b>')
        rows.append([btn('−',f'ci:{i["id"]}:-1:{page}'),btn(f'№{index} · {i["qty"]} шт.','noop'),btn('+',f'ci:{i["id"]}:1:{page}'),btn('✕',f'cd:{i["id"]}:{page}')])
    if len(items)>3:rows.append([btn('‹',f'cp:{max(0,page-1)}'),btn(f'{page+1}/{math.ceil(len(items)/3)}','noop'),btn('›',f'cp:{min((len(items)-1)//3,page+1)}')])
    lines.append(f'<b>Всего: {sum(i["qty"]*i["price"] for i in items)} ₽</b>\nТовар резервируется при подтверждении заказа.')
    rows.extend([[btn('✅ Оформить заказ','checkout')],[btn('Ещё шаурму','catalog'),btn('Меню','menu')]])
    await show(cb,'\n\n'.join(lines),rows)


@router.callback_query(F.data.regexp(r'^(p|add):\d+$'))
async def open_product(cb):await product(cb,store.new_recipe(cb.from_user.id,int(cb.data.split(':')[1])))


@router.callback_query(F.data.regexp(r'^(ra|rr|rq|radd):'))
async def customize(cb):
    parts=cb.data.split(':');action=parts[0];uid=cb.from_user.id;token=parts[1]
    if action=='radd':
        added=store.add_recipe(uid,token)
        return await basket(cb,note='Рецепт добавлен.' if added else 'Уже добавлено. Количество можно изменить ниже.')
    r=store.recipe(uid,token)
    if action=='ra':r=store.toggle(uid,token,'add',parts[2])
    elif action=='rr':
        names=stock.product_meta(r['product_id'])['ingredients'];index=int(parts[2])
        if not 0<=index<len(names):raise stock.ShopError('Ингредиент не найден')
        r=store.toggle(uid,token,'remove',names[index])
    else:
        if parts[2] not in ('-1','1'):raise stock.ShopError('Неверное количество')
        r=store.recipe_qty(uid,token,int(parts[2]))
    await product(cb,r)


@router.callback_query(F.data=='cart')
async def cart(cb,state):
    await state.clear()
    await basket(cb)


@router.callback_query(F.data.regexp(r'^(ci|cd|cp):'))
async def change_cart(cb):
    p=cb.data.split(':');uid=cb.from_user.id
    if p[0]=='cp':return await basket(cb,int(p[1]))
    if p[0]=='ci':
        if p[2] not in ('-1','1'):raise stock.ShopError('Неверное количество')
        store.change_line(uid,int(p[1]),int(p[2]));page=int(p[3])
    else:store.change_line(uid,int(p[1]));page=int(p[2])
    await basket(cb,page)
