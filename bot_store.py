"""Долговечные состояния диалога и корзина с вариантами для Telegram."""
import json
import secrets

import db
import finance
import stock


def init():
    c = db.get_conn()
    try:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS bot_sessions(user_id INTEGER PRIMARY KEY REFERENCES users(id),data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_panels(user_id INTEGER PRIMARY KEY REFERENCES users(id),chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,image_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_cart_lines(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id),product_id INTEGER NOT NULL REFERENCES products(id),qty INTEGER NOT NULL CHECK(qty BETWEEN 1 AND 20),options_json TEXT NOT NULL,UNIQUE(user_id,product_id,options_json));
        CREATE TABLE IF NOT EXISTS bot_recipe_adds(user_id INTEGER NOT NULL REFERENCES users(id),token TEXT NOT NULL,PRIMARY KEY(user_id,token));
        ''')
    finally:
        c.close()
    # Старые стандартные позиции переносим без потери количества, только один раз.
    with finance.tx() as c:
        for row in c.execute('SELECT * FROM cart_items').fetchall():
            c.execute('INSERT INTO bot_cart_lines(user_id,product_id,qty,options_json) VALUES(?,?,?,?) ON CONFLICT(user_id,product_id,options_json) DO UPDATE SET qty=MIN(20,bot_cart_lines.qty+excluded.qty)',
                      (row['user_id'],row['product_id'],row['qty'],options([],[])))
        c.execute('DELETE FROM cart_items')


def options(add, remove):
    return json.dumps(dict(add=sorted(set(add)),remove=sorted(set(remove))),ensure_ascii=False,sort_keys=True)


def session(uid):
    c=db.get_conn()
    try:
        row=c.execute('SELECT data FROM bot_sessions WHERE user_id=?',(uid,)).fetchone()
        return json.loads(row['data']) if row else {}
    finally:c.close()


def save(uid,data):
    with finance.tx() as c:
        c.execute('INSERT INTO bot_sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data',(uid,json.dumps(data,ensure_ascii=False)))


def update(uid,**kwargs):
    data=session(uid);data.update(kwargs);save(uid,data);return data


def panel(uid):
    c=db.get_conn()
    try:
        row=c.execute('SELECT * FROM bot_panels WHERE user_id=?',(uid,)).fetchone()
        return dict(row) if row else None
    finally:c.close()


def save_panel(uid,chat_id,message_id,image_id):
    with finance.tx() as c:
        c.execute('INSERT INTO bot_panels VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,message_id=excluded.message_id,image_id=excluded.image_id',(uid,chat_id,message_id,image_id))


def new_recipe(uid,pid):
    p=db.get_product(pid)
    if not p or not p['available']:
        raise stock.ShopError('Товар недоступен',404)
    r=dict(product_id=pid,qty=1,add=[],remove=[],token=secrets.token_hex(4))
    update(uid,recipe=r,stage=None)
    return r


def recipe(uid,token):
    r=session(uid).get('recipe')
    if not r or r['token']!=token:
        raise stock.ShopError('Эта карточка устарела. Откройте товар из каталога.',409)
    return r


def toggle(uid,token,kind,value):
    r=recipe(uid,token);meta=stock.product_meta(r['product_id'])
    allowed=meta['additions'] if kind=='add' else meta['ingredients']
    if value not in allowed:
        raise stock.ShopError('Недопустимый ингредиент')
    if value in r[kind]:r[kind].remove(value)
    else:r[kind].append(value)
    update(uid,recipe=r)
    return r


def recipe_qty(uid,token,delta):
    r=recipe(uid,token);r['qty']=max(1,min(20,r['qty']+delta));update(uid,recipe=r);return r


def priced(raw):
    c=db.get_conn()
    try:return stock.canonical_items(c,raw)
    finally:c.close()


def recipe_price(r):
    return priced([{k:r[k] for k in ('product_id','qty','add','remove')}])[0]


def add_recipe(uid,token):
    r=recipe(uid,token);recipe_price(r)
    with finance.tx() as c:
        if not c.execute('INSERT OR IGNORE INTO bot_recipe_adds VALUES(?,?)',(uid,token)).rowcount:
            return False
        opt=options(r['add'],r['remove'])
        old=c.execute('SELECT qty FROM bot_cart_lines WHERE user_id=? AND product_id=? AND options_json=?',(uid,r['product_id'],opt)).fetchone()
        if old and old['qty']+r['qty']>20:
            raise stock.ShopError('Не больше 20 порций одного рецепта. Измените количество в корзине.')
        if c.execute('SELECT COUNT(*) FROM bot_cart_lines WHERE user_id=?',(uid,)).fetchone()[0]>=50 and not old:
            raise stock.ShopError('До 50 вариантов в одном заказе')
        c.execute('INSERT INTO bot_cart_lines(user_id,product_id,qty,options_json) VALUES(?,?,?,?) ON CONFLICT(user_id,product_id,options_json) DO UPDATE SET qty=bot_cart_lines.qty+excluded.qty',
                  (uid,r['product_id'],r['qty'],opt))
    return True


def cart(uid):
    c=db.get_conn()
    try:
        rows=[dict(r) for r in c.execute('SELECT * FROM bot_cart_lines WHERE user_id=? ORDER BY id',(uid,))]
        result=[]
        for row in rows:
            raw=dict(product_id=row['product_id'],qty=row['qty'],**json.loads(row['options_json']))
            item=stock.canonical_items(c,[raw])[0]
            result.append(dict(row,**{k:v for k,v in item.items() if k not in row},raw=raw,name=item['product_name']))
        return result
    finally:c.close()


def change_line(uid,line_id,delta=None):
    with finance.tx() as c:
        row=c.execute('SELECT * FROM bot_cart_lines WHERE id=? AND user_id=?',(line_id,uid)).fetchone()
        if not row:raise stock.ShopError('Позиция уже удалена. Корзина обновлена.',409)
        quantity=0 if delta is None else min(20,row['qty']+delta)
        if quantity<=0:c.execute('DELETE FROM bot_cart_lines WHERE id=? AND user_id=?',(line_id,uid))
        else:c.execute('UPDATE bot_cart_lines SET qty=? WHERE id=? AND user_id=?',(quantity,line_id,uid))


def prepare_checkout(uid,name):
    rows=cart(uid)
    if not rows:raise stock.ShopError('Корзина пуста')
    return update(uid,stage='delivery',name=name,checkout=dict(items=[r['raw'] for r in rows],cart_snapshot=[{k:r[k] for k in ('id','product_id','qty','options_json')} for r in rows],request_key='bot-'+secrets.token_hex(16)))


def confirm(uid,token):
    data=session(uid);checkout=data.get('checkout')
    if not checkout or checkout['request_key']!=token:
        raise stock.ShopError('Подтверждение устарело. Откройте корзину.',409)
    result=stock.checkout(uid,checkout['items'],data['name'],data.get('delivery','Самовывоз'),token,
                          data.get('note',''),bot_cart_snapshot=checkout['cart_snapshot'])
    update(uid,stage=None,last_order=result['id'])
    return result
