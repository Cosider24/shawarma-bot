"""Единые транзакции склада, продаж и возвратов. Деньги в копейках, UTC epoch."""
import hashlib
import json
import os
import sqlite3
import time
import uuid

import db
import config
import finance


class ShopError(finance.FinanceError):
    pass


def now():
    return time.time()


def migrate(c):
    for table, name, definition in [
        ('order_items', 'product_id', 'INTEGER REFERENCES products(id)'),
        ('order_items', 'options_json', "TEXT NOT NULL DEFAULT '{}'"),
        ('orders', 'sale_accepted', 'INTEGER NOT NULL DEFAULT 0'),
        ('orders', 'stock_managed', 'INTEGER NOT NULL DEFAULT 0'),
        ('orders', 'note', "TEXT NOT NULL DEFAULT ''"),
    ]:
        if name not in db._columns(c, table):
            c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS stock (
      product_id INTEGER PRIMARY KEY REFERENCES products(id),
      on_hand INTEGER NOT NULL DEFAULT 0 CHECK(typeof(on_hand)='integer' AND on_hand>=0),
      reserved INTEGER NOT NULL DEFAULT 0 CHECK(typeof(reserved)='integer' AND reserved>=0 AND reserved<=on_hand));
    CREATE TABLE IF NOT EXISTS reservations (
      id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
      state TEXT NOT NULL CHECK(state IN ('active','consumed','released')),
      expires_at REAL NOT NULL, release_reason TEXT);
    CREATE INDEX IF NOT EXISTS ix_reservation_expiry ON reservations(state,expires_at);
    CREATE TABLE IF NOT EXISTS reservation_items (
      reservation_id INTEGER NOT NULL REFERENCES reservations(id),
      product_id INTEGER NOT NULL REFERENCES products(id), qty INTEGER NOT NULL CHECK(typeof(qty)='integer' AND qty>0),
      PRIMARY KEY(reservation_id,product_id));
    CREATE TABLE IF NOT EXISTS stock_journal (
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
      kind TEXT NOT NULL, delta_hand INTEGER NOT NULL, delta_reserved INTEGER NOT NULL,
      order_id INTEGER REFERENCES orders(id), occurred_at REAL NOT NULL,
      author TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS checkout_requests (
      user_id INTEGER NOT NULL REFERENCES users(id), request_key TEXT NOT NULL,
      fingerprint TEXT NOT NULL, order_id INTEGER NOT NULL REFERENCES orders(id),
      PRIMARY KEY(user_id,request_key));
    CREATE TABLE IF NOT EXISTS refund_jobs (
      payment_id INTEGER PRIMARY KEY REFERENCES payments(id),
      state TEXT NOT NULL CHECK(state IN ('refund_pending','refund_succeeded')),
      operation_key TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, lease_until REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS simulator_refunds (
      operation_key TEXT PRIMARY KEY, refund_id TEXT NOT NULL UNIQUE,
      amount_kop INTEGER NOT NULL, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS simulator_faults (
      payment_id INTEGER PRIMARY KEY REFERENCES payments(id), errors INTEGER NOT NULL DEFAULT 0,
      lose_response INTEGER NOT NULL DEFAULT 0);
    CREATE TRIGGER IF NOT EXISTS one_payment_per_new_order BEFORE INSERT ON payments
    WHEN EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND stock_managed=1)
      AND EXISTS(SELECT 1 FROM payments WHERE order_id=NEW.order_id)
    BEGIN SELECT RAISE(ABORT,'one payment per stock order'); END;
    ''')
    # Старые заказы не расходуют новый склад, но их себестоимость остаётся в отчётах.
    c.execute("UPDATE orders SET sale_accepted=1 WHERE stock_managed=0 AND pay_status IN ('paid','refunded')")
    c.execute('INSERT OR IGNORE INTO stock(product_id) SELECT id FROM products')
    if 'first_attempt_at' not in db._columns(c,'refund_jobs'):
        c.execute('ALTER TABLE refund_jobs ADD COLUMN first_attempt_at REAL NOT NULL DEFAULT 0')
    # Resume historical pending refunds with their original provider key.
    # Unknown previous attempt time is treated conservatively for external API retries.
    c.execute("INSERT OR IGNORE INTO refund_jobs(payment_id,state,operation_key,first_attempt_at) SELECT id,'refund_pending','refund-'||id,1 FROM payments WHERE status='refund_pending'")


def integer(value, label, lo=1, hi=10000):
    if type(value) is not int or not lo <= value <= hi:
        raise ShopError(f'{label}: целое число от {lo} до {hi}')
    return value


def key(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 100:
        raise ShopError('Нужен ключ запроса длиной 8–100 символов')
    return value


def movement(c, pid, kind, hand, reserved, oid, author, operation):
    c.execute('UPDATE stock SET on_hand=on_hand+?,reserved=reserved+? WHERE product_id=?',
              (hand, reserved, pid))
    c.execute('INSERT INTO stock_journal(product_id,kind,delta_hand,delta_reserved,order_id,occurred_at,author,operation_key) VALUES(?,?,?,?,?,?,?,?)',
              (pid, kind, hand, reserved, oid, now(), str(author), operation))


def receive(pid, qty, author, request_key):
    integer(pid, 'Товар'); integer(qty, 'Количество'); key(request_key)
    operation = f'receive:{author}:{request_key}'
    with finance.tx() as c:
        old = c.execute('SELECT * FROM stock_journal WHERE operation_key=?', (operation,)).fetchone()
        if old:
            if old['product_id'] != pid or old['delta_hand'] != qty:
                raise ShopError('Ключ уже использован для другого поступления', 409)
            return {'duplicate': True}
        if not c.execute('SELECT 1 FROM stock WHERE product_id=?', (pid,)).fetchone():
            raise ShopError('Товар не найден', 404)
        movement(c, pid, 'receipt', qty, 0, None, author, operation)
    return {'duplicate': False}


def catalog():
    c = db.get_conn()
    try:
        rows = [dict(r) for r in c.execute('SELECT p.*,s.on_hand,s.reserved,s.on_hand-s.reserved AS stock_available FROM products p JOIN stock s ON s.product_id=p.id WHERE p.available=1 ORDER BY p.id')]
        for r in rows:
            r.update(product_meta(r['id']))
        return rows
    finally:
        c.close()


ADDITIONS = {
    'jalapeno': {'name': 'Халапеньо', 'price': 30, 'cost_kop': 1000},
    'cheese': {'name': 'Двойной сыр', 'price': 40, 'cost_kop': 1800},
    'chicken': {'name': 'Больше начинки', 'price': 70, 'cost_kop': 3500},
    'mushrooms': {'name': 'Грибы', 'price': 40, 'cost_kop': 1600},
}
INGREDIENTS = {
    1: ['Курица','Томаты','Огурцы','Капуста','Лук','Чесночный соус','Майонез'],
    2: ['Говядина','Лук','Томаты','Чесночный соус'],
    3: ['Курица','Сыр','Огурцы','Сливочный соус'],
    4: ['Курица','Халапеньо','Томаты','Капуста','Острый соус'],
    5: ['Фалафель','Хумус','Томаты','Огурцы','Тахини'],
}
WITHOUT = {'Курица':'курицы','Томаты':'томатов','Огурцы':'огурцов','Капуста':'капусты','Лук':'лука',
           'Чесночный соус':'чесночного соуса','Говядина':'говядины','Сыр':'сыра',
           'Сливочный соус':'сливочного соуса','Халапеньо':'халапеньо','Острый соус':'острого соуса',
           'Фалафель':'фалафеля','Хумус':'хумуса','Тахини':'тахини','Майонез':'майонеза'}


def product_meta(pid):
    return {'image': f'/assets/product-{pid if pid in range(1,7) else 1}.svg',
            'ingredients': INGREDIENTS.get(pid, []),
            'without_labels': WITHOUT,
            'additions': ADDITIONS if pid in INGREDIENTS else {},
            'category': 'Напитки' if pid == 6 else 'Шаурма',
            'allergens': 'Возможны следы аллергенов; уточняйте у кухни. Лаваш содержит глютен.' if pid != 6 else 'Клюква, сахар, вода',
            'weight': '500 мл' if pid == 6 else '≈ 380 г'}


def canonical_items(c, raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
        raise ShopError('Корзина должна содержать от 1 до 50 позиций')
    items = []
    for line in raw:
        if not isinstance(line, dict) or set(line) - {'product_id','qty','add','remove'}:
            raise ShopError('Недопустимые поля позиции')
        pid = integer(line.get('product_id'), 'Товар'); qty = integer(line.get('qty'), 'Количество', hi=20)
        p = c.execute('SELECT * FROM products WHERE id=? AND available=1', (pid,)).fetchone()
        if not p:
            raise ShopError('Товар недоступен', 409)
        add, remove = line.get('add', []), line.get('remove', [])
        if not isinstance(add, list) or not isinstance(remove, list) or any(not isinstance(v,str) for v in add+remove):
            raise ShopError('Неверный состав')
        add, remove = sorted(set(add)), sorted(set(remove))
        if any(a not in product_meta(pid)['additions'] for a in add) or any(a not in INGREDIENTS.get(pid,[]) for a in remove):
            raise ShopError('Такой добавки или ингредиента нет у товара')
        price = p['price'] + sum(ADDITIONS[a]['price'] for a in add)
        cost = p['cost_kop'] + sum(ADDITIONS[a]['cost_kop'] for a in add)
        options = json.dumps({'add': add, 'remove': remove}, ensure_ascii=False, sort_keys=True)
        suffix = '; '.join(([f'+ {ADDITIONS[a]["name"]}' for a in add] + [f'без {WITHOUT.get(a,a)}' for a in remove]))
        items.append(dict(product_id=pid,qty=qty,price=price,cost_kop=cost,
                          product_name=p['name'] + (f' ({suffix})' if suffix else ''),options_json=options))
    return sorted(items, key=lambda v:(v['product_id'],v['options_json'],v['qty']))


def checkout(uid, raw, customer_name, delivery, request_key, note='', *, bot_cart_snapshot=None):
    key(request_key)
    if not isinstance(customer_name,str) or not 1 <= len(customer_name.strip()) <= 50:
        raise ShopError('Укажите имя до 50 символов')
    if delivery not in ('Самовывоз','Поесть на месте'):
        raise ShopError('Выберите способ получения')
    if not isinstance(note,str) or len(note)>300:
        raise ShopError('Комментарий до 300 символов')
    if not isinstance(raw,list) or not 1 <= len(raw) <= 50 or any(not isinstance(i,dict) or set(i)-{'product_id','qty','add','remove'} for i in raw):
        raise ShopError('Неверная корзина или недопустимые поля позиции')
    # Fingerprint request content, not mutable catalogue prices.
    try:
        request_lines = sorted([{'product_id': i['product_id'], 'qty': i['qty'],
                                 'add': sorted(set(i.get('add',[]))), 'remove': sorted(set(i.get('remove',[])))} for i in raw],
                               key=lambda i:json.dumps(i,sort_keys=True))
    except (TypeError,KeyError,AttributeError):
        raise ShopError('Неверная корзина')
    fingerprint = hashlib.sha256(json.dumps([request_lines,customer_name.strip(),delivery,note],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    with finance.tx() as c:
        old = c.execute('SELECT * FROM checkout_requests WHERE user_id=? AND request_key=?',(uid,request_key)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise ShopError('Этот ключ уже использован с другим составом',409)
            return {'id':old['order_id'],'duplicate':True}
        if bot_cart_snapshot is not None:
            actual=[dict(r) for r in c.execute('SELECT id,product_id,qty,options_json FROM bot_cart_lines WHERE user_id=? ORDER BY id',(uid,))]
            if actual!=bot_cart_snapshot:
                raise ShopError('Корзина изменилась. Проверьте её и подтвердите заново.',409)
        items = canonical_items(c,raw)
        totals = {}
        for i in items:
            totals[i['product_id']] = totals.get(i['product_id'],0) + i['qty']
        for pid, qty in sorted(totals.items()):
            s = c.execute('SELECT on_hand-reserved FROM stock WHERE product_id=?',(pid,)).fetchone()
            if not s or s[0] < qty:
                raise ShopError('Сейчас нет в наличии нужного количества. Обновите корзину.',409)
        t = now()
        oid = c.execute("INSERT INTO orders(user_id,customer_name,delivery,payment,total,status,created_at,pay_status,stock_managed,note) VALUES(?,?,?,'Учебная оплата',?,'Новый',?,'awaiting_payment',1,?)",
                        (uid,customer_name.strip(),delivery,sum(i['price']*i['qty'] for i in items),finance.now_iso(),note)).lastrowid
        rid = c.execute("INSERT INTO reservations(order_id,state,expires_at) VALUES(?,'active',?)",(oid,t+config.RESERVATION_SECONDS)).lastrowid
        for i in items:
            c.execute('INSERT INTO order_items(order_id,product_id,product_name,price,cost_kop,qty,options_json) VALUES(?,?,?,?,?,?,?)',
                      (oid,i['product_id'],i['product_name'],i['price'],i['cost_kop'],i['qty'],i['options_json']))
        for pid,qty in sorted(totals.items()):
            c.execute('INSERT INTO reservation_items VALUES(?,?,?)',(rid,pid,qty))
            movement(c,pid,'reserve',0,qty,oid,uid,f'reserve:{oid}:{pid}')
        if config.TEST_MODE and os.getenv('SHOP_FAILPOINT') == 'checkout_before_commit':
            os._exit(86)
        c.execute('INSERT INTO checkout_requests VALUES(?,?,?,?)',(uid,request_key,fingerprint,oid))
        c.execute('DELETE FROM cart_items WHERE user_id=?',(uid,))
        if bot_cart_snapshot is not None:
            c.execute('DELETE FROM bot_cart_lines WHERE user_id=?',(uid,))
        return {'id':oid,'duplicate':False}


def release(c, r, reason):
    if r['state'] != 'active':
        return False
    for i in c.execute('SELECT * FROM reservation_items WHERE reservation_id=? ORDER BY product_id',(r['id'],)).fetchall():
        movement(c,i['product_id'],'release',0,-i['qty'],r['order_id'],'system',f'release:{r["order_id"]}:{i["product_id"]}')
    c.execute("UPDATE reservations SET state='released',release_reason=? WHERE id=?",(reason,r['id']))
    c.execute("UPDATE orders SET pay_status=?,status='Отменён' WHERE id=?",(reason,r['order_id']))
    return True


def cancel(oid, uid, manager=False):
    with finance.tx() as c:
        o = c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not o or (not manager and o['user_id'] != uid):
            raise ShopError('Заказ не найден',404)
        r = c.execute('SELECT * FROM reservations WHERE order_id=?',(oid,)).fetchone()
        if not r:
            raise ShopError('Старый заказ: обработайте через кабинет',409)
        if r['state'] == 'consumed':
            raise ShopError('Заказ оплачен. Нужен полный возврат.',409)
        return {'released':release(c,r,'canceled')}


def expire():
    with finance.tx() as c:
        rs = c.execute("SELECT * FROM reservations WHERE state='active' AND expires_at<=?",(now(),)).fetchall()
        for r in rs:
            release(c,r,'expired')
        return len(rs)


def create_payment(oid,uid,provider):
    with finance.tx() as c:
        o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not o or o['user_id'] != uid:
            raise ShopError('Заказ не найден',404)
        r=c.execute('SELECT * FROM reservations WHERE order_id=?',(oid,)).fetchone()
        if o['pay_status'] != 'awaiting_payment' or (r and (r['state']!='active' or now()>=r['expires_at'])):
            raise ShopError('Резерв закончился или заказ уже обработан',409)
        old=c.execute('SELECT * FROM payments WHERE order_id=? ORDER BY id LIMIT 1',(oid,)).fetchone()
        if old:
            if old['provider'] != provider or old['status'] != 'pending':
                raise ShopError('Платёж уже существует и не ожидает оплаты',409)
            return dict(old)
        t=finance.now_iso()
        pid=c.execute("INSERT INTO payments(order_id,provider,amount_kop,currency,status,created_at,updated_at) VALUES(?,?,?,'RUB','pending',?,?)",(oid,provider,o['total']*100,t,t)).lastrowid
        return dict(id=pid,order_id=oid,amount_kop=o['total']*100,currency='RUB')


def queue_refund(c,pid):
    c.execute("UPDATE payments SET status='refund_pending',updated_at=? WHERE id=? AND status='succeeded'",(finance.now_iso(),pid))
    c.execute("INSERT OR IGNORE INTO refund_jobs(payment_id,state,operation_key) VALUES(?,'refund_pending',?)",(pid,f'refund-{uuid.uuid4()}'))


def payment_success(pid, amount, currency, provider_id=None, tg_id=None):
    with finance.tx() as c:
        p=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
        if not p:
            raise ShopError('Платёж не найден',404)
        o=c.execute('SELECT * FROM orders WHERE id=?',(p['order_id'],)).fetchone()
        if type(amount) is not int or amount!=p['amount_kop'] or amount!=o['total']*100 or currency!=p['currency']:
            raise ShopError('Сумма или валюта не совпадает',409)
        result=dict(duplicate=p['status'] in ('succeeded','refund_pending','refunded'),order_id=o['id'],amount_kop=amount)
        if result['duplicate']:
            return dict(result,conflict=o['pay_status']=='payment_conflict')
        r=c.execute('SELECT * FROM reservations WHERE order_id=?',(o['id'],)).fetchone()
        accepted = (r is None and not o['stock_managed']) or (r and r['state']=='active' and now()<r['expires_at'])
        if r and accepted:
            for i in c.execute('SELECT * FROM reservation_items WHERE reservation_id=? ORDER BY product_id',(r['id'],)).fetchall():
                movement(c,i['product_id'],'sale',-i['qty'],-i['qty'],o['id'],'payment',f'sale:{o["id"]}:{i["product_id"]}')
            c.execute("UPDATE reservations SET state='consumed' WHERE id=?",(r['id'],))
        elif r:
            release(c,r,'expired' if now()>=r['expires_at'] else 'canceled')
        t=finance.now_iso()
        c.execute("UPDATE payments SET status='succeeded',provider_payment_id=?,tg_charge_id=?,updated_at=? WHERE id=?",(provider_id,tg_id,t,pid))
        c.execute('UPDATE orders SET pay_status=?,sale_accepted=? WHERE id=?',('paid' if accepted else 'payment_conflict',int(bool(accepted)),o['id']))
        c.execute("INSERT INTO finance_operations(type,amount_kop,occurred_at,order_id,payment_id,external_id,author_id,created_at) VALUES('receipt',?,?,?,?,?,0,?)",(amount,t,o['id'],pid,f'receipt:payment:{pid}',t))
        if not accepted:
            queue_refund(c,pid)
        return dict(result,conflict=not accepted)


def request_refund(oid):
    with finance.tx() as c:
        p=c.execute("SELECT * FROM payments WHERE order_id=? AND status IN ('succeeded','refund_pending','refunded')",(oid,)).fetchone()
        if not p:
            raise ShopError('Нет успешного платежа',409)
        if p['status']!='refunded':
            queue_refund(c,p['id'])
        return dict(p)


def complete_refund(pid,refund_id=None):
    with finance.tx() as c:
        p=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
        if not p or p['status'] not in ('refund_pending','refunded'):
            raise ShopError('Нет ожидающего возврата',409)
        result=dict(duplicate=p['status']=='refunded',order_id=p['order_id'],amount_kop=p['amount_kop'])
        if result['duplicate']:
            return result
        o=c.execute('SELECT * FROM orders WHERE id=?',(p['order_id'],)).fetchone()
        r=c.execute('SELECT * FROM reservations WHERE order_id=?',(o['id'],)).fetchone()
        if o['sale_accepted'] and r and r['state']=='consumed':
            for i in c.execute('SELECT * FROM reservation_items WHERE reservation_id=? ORDER BY product_id',(r['id'],)).fetchall():
                movement(c,i['product_id'],'return',i['qty'],0,o['id'],'refund',f'return:{o["id"]}:{i["product_id"]}')
        t=finance.now_iso()
        c.execute("UPDATE payments SET status='refunded',refund_id=?,refund_error=NULL,updated_at=? WHERE id=?",(refund_id,t,pid))
        c.execute("UPDATE orders SET pay_status=CASE WHEN pay_status='payment_conflict' THEN pay_status ELSE 'refunded' END WHERE id=?",(o['id'],))
        c.execute("INSERT INTO finance_operations(type,amount_kop,occurred_at,order_id,payment_id,external_id,author_id,created_at) VALUES('refund',?,?,?,?,?,0,?)",(p['amount_kop'],t,o['id'],pid,f'refund:payment:{pid}',t))
        c.execute("UPDATE refund_jobs SET state='refund_succeeded',last_error=NULL,lease_until=0 WHERE payment_id=?",(pid,))
        return result


def refund_error(pid,error):
    with finance.tx() as c:
        c.execute('UPDATE refund_jobs SET last_error=?,next_attempt=?,lease_until=0 WHERE payment_id=? AND state=\'refund_pending\'',(str(error)[:300],now()+config.WORKER_INTERVAL,pid))
        c.execute("UPDATE payments SET refund_error=? WHERE id=? AND status='refund_pending'",(str(error)[:300],pid))


def dashboard():
    c=db.get_conn()
    try:
        return dict(stock=catalog(),reservations=[dict(r) for r in c.execute('SELECT r.*,o.user_id,o.customer_name,o.total,o.pay_status FROM reservations r JOIN orders o ON o.id=r.order_id ORDER BY r.id DESC LIMIT 200')],
                    journal=[dict(r) for r in c.execute('SELECT j.*,p.name FROM stock_journal j JOIN products p ON p.id=j.product_id ORDER BY j.id DESC LIMIT 300')],
                    refunds=[dict(r) for r in c.execute('SELECT j.*,p.order_id,p.amount_kop,p.status FROM refund_jobs j JOIN payments p ON p.id=j.payment_id ORDER BY j.payment_id DESC LIMIT 200')],server_time=now())
    finally:
        c.close()


def customer_order(oid,uid):
    c=db.get_conn()
    try:
        o=c.execute('SELECT * FROM orders WHERE id=? AND user_id=?',(oid,uid)).fetchone()
        if not o:
            raise ShopError('Заказ не найден',404)
        r=c.execute('SELECT * FROM reservations WHERE order_id=?',(oid,)).fetchone()
        return dict(order=dict(o),items=[dict(r) for r in c.execute('SELECT * FROM order_items WHERE order_id=?',(oid,))],reservation=dict(r) if r else None,
                    payments=[dict(r) for r in c.execute('SELECT id,status,amount_kop,refund_error FROM payments WHERE order_id=?',(oid,))],server_time=now())
    finally:
        c.close()
