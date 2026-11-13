"""Приёмка: настоящий HTTP, два процесса, SQLite, общий стартовый барьер.

python -m tests.acceptance --rounds 5 --buyers 20 --stock 1
"""
import concurrent.futures
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parents[1]
TOKEN='123456:TEST_TOKEN_NOT_REAL'
ROUNDS=5
CUSTOM_BUYERS=20
CUSTOM_STOCK=1
RESERVE_SECONDS=120
EVIDENCE=[]


def auth(uid):
    fields={'auth_date':str(int(time.time())), 'user':json.dumps({'id':uid,'first_name':f'Test-{uid}'})}
    check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    fields['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return 'tma '+urlencode(fields)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.temp=None;self.processes=[];self.logs=[];self.fresh()

    def stop(self):
        for p in self.processes:
            if p.poll() is None:p.terminate()
        for p in self.processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        self.processes=[]
        for f in self.logs:f.close()
        self.logs=[]

    def tearDown(self):
        self.stop()
        if self.temp:self.temp.cleanup()

    def fresh(self,seconds=None):
        seconds=RESERVE_SECONDS if seconds is None else seconds
        self.stop()
        if self.temp:self.temp.cleanup()
        self.temp=tempfile.TemporaryDirectory(prefix='shawarma-acceptance-')
        self.database=Path(self.temp.name)/'acceptance.sqlite3'
        self.clock=Path(self.temp.name)/'clock.txt';self.clock.write_text('2000000000')
        self.seconds=seconds;self.ports=[free_port(),free_port()]
        while self.ports[0]==self.ports[1]:self.ports[1]=free_port()
        self.boot()
        # Вымышленный каталог для требований 1200 / 700; исходная БД не открывается.
        self.sql('UPDATE products SET price=1200,cost_kop=70000 WHERE id=1')

    def boot(self,failpoint='',both=True):
        self.stop()
        for port in self.ports[:2 if both else 1]:
            env=dict(os.environ,BOT_TOKEN=TOKEN,MANAGER_IDS='111',PAYMENT_MODE='simulator',SHOP_DB=str(self.database),SHOP_TEST_MODE='1',
                     TEST_CLOCK_FILE=str(self.clock),WEB_PORT=str(port),RESERVATION_SECONDS=str(self.seconds),WORKER_INTERVAL='0.2',SHOP_FAILPOINT=failpoint)
            log=open(Path(self.temp.name)/f'server-{port}.log','ab');self.logs.append(log)
            # Windows venv launcher can outlive terminate() as a separate child.
            # Launch the actual interpreter, retaining this environment's packages.
            executable=getattr(sys,'_base_executable',sys.executable) if os.name=='nt' else sys.executable
            env['PYTHONPATH']=os.pathsep.join(sys.path)
            p=subprocess.Popen([executable,str(ROOT/'tests/service.py')],env=env,cwd=ROOT,stdout=log,stderr=log);self.processes.append(p)
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if p.poll() is not None:
                    raise AssertionError(f'Сервер завершился: {(Path(self.temp.name)/f"server-{port}.log").read_text(errors="replace")}')
                try:
                    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=.3)
                    try:
                        conn.request('GET','/shop');response=conn.getresponse();response.read()
                        if response.status==200:break
                    finally:conn.close()
                except (OSError,http.client.HTTPException):time.sleep(.05)
            else:raise AssertionError('Сервер не запустился')

    def sql(self,statement,args=()):
        c=sqlite3.connect(self.database,timeout=15)
        try:
            c.row_factory=sqlite3.Row
            rows=[dict(r) for r in c.execute(statement,args).fetchall()]
            c.commit()
            return rows
        finally:
            c.close()

    def scalar(self,statement,args=()):
        rows=self.sql(statement,args)
        return next(iter(rows[0].values()))

    def call(self,path,body=None,uid=111,port=None,authorization=True):
        headers={'Content-Type':'application/json'}
        if authorization:headers['Authorization']=auth(uid)
        connection=http.client.HTTPConnection('127.0.0.1',port or self.ports[0],timeout=25)
        try:
            connection.request('POST' if body is not None else 'GET',path,body=json.dumps(body).encode() if body is not None else None,headers=headers)
            r=connection.getresponse()
            return r.status,json.loads(r.read())
        finally:connection.close()

    def ok(self,path,body=None,uid=111):
        status,r=self.call(path,body,uid)
        self.assertEqual(status,200,(path,status,r));return r

    def receive(self,qty,pid=1):
        self.ok('/api/stock/receive',dict(product_id=pid,qty=qty,request_key=f'initial-product-{pid}'))

    def checkout(self,uid=1,qty=1,key=None,items=None,port=None):
        return self.call('/api/shop/checkout',dict(items=items or [dict(product_id=1,qty=qty)],name='Тестовый покупатель',delivery='Самовывоз',request_key=key or f'checkout-{uid}-unique'),uid,port)

    def order(self,uid=1,qty=1,key=None):
        status,r=self.checkout(uid,qty,key);self.assertEqual(status,200,r);return r['id']

    def payment(self,oid,uid=1):
        return self.ok(f'/api/shop/orders/{oid}/pay',{},uid)['id']

    def paid(self,pid,uid=1):
        return self.ok(f'/api/shop/payments/{pid}/simulate',{'result':'succeeded'},uid)

    def stock(self,pid=1):
        row=self.sql('SELECT * FROM stock WHERE product_id=?',(pid,))[0]
        return row['on_hand'],row['reserved'],row['on_hand']-row['reserved']

    def clock_at(self,value):
        # Atomic replace avoids a partially written controlled clock.
        tmp=self.clock.with_suffix('.tmp');tmp.write_text(str(value));os.replace(tmp,self.clock)

    def eventually(self,fn,expected,seconds=5):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            got=fn()
            if got==expected:return got
            time.sleep(.05)
        self.assertEqual(got,expected)

    def parallel(self,functions):
        barrier=threading.Barrier(len(functions))
        def run(fn):barrier.wait(timeout=20);return fn()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run,functions))

    def invariants(self):
        self.assertEqual(self.scalar('SELECT count(*) FROM stock WHERE on_hand<0 OR reserved<0 OR reserved>on_hand'),0)
        self.assertEqual(self.scalar("SELECT count(*) FROM stock s WHERE reserved != COALESCE((SELECT SUM(ri.qty) FROM reservation_items ri JOIN reservations r ON r.id=ri.reservation_id WHERE ri.product_id=s.product_id AND r.state='active'),0)"),0)
        self.assertEqual(self.scalar('SELECT count(*) FROM stock s WHERE on_hand != COALESCE((SELECT SUM(delta_hand) FROM stock_journal j WHERE j.product_id=s.product_id),0) OR reserved != COALESCE((SELECT SUM(delta_reserved) FROM stock_journal j WHERE j.product_id=s.product_id),0)'),0)
        self.assertEqual(self.scalar('PRAGMA integrity_check'),'ok')
        self.assertEqual(self.sql('PRAGMA foreign_key_check'),[])
        self.assertEqual(self.scalar("SELECT count(*) FROM (SELECT order_id,type,count(*) n FROM finance_operations WHERE type IN ('receipt','refund') GROUP BY order_id,type HAVING n>1)"),0)

    def summary(self):return self.ok('/api/summary?from=2000-01-01&to=2099-12-31')

    def record(self,scenario,expected,got):
        EVIDENCE.append(dict(scenario=scenario,expected=expected,actual=got,stock=self.sql('SELECT * FROM stock'),
                             orders=self.sql('SELECT id,user_id,pay_status,sale_accepted,total FROM orders'),
                             journal=self.sql('SELECT * FROM stock_journal'),finance=self.sql('SELECT type,amount_kop,order_id,payment_id FROM finance_operations')))

    def test_01_manual_chain(self):
        self.receive(10);got=[self.stock()]
        a=self.order(qty=3);got.append(self.stock());p=self.payment(a);self.paid(p);got.append(self.stock())
        self.order(2,qty=2);got.append(self.stock());self.clock_at((2000000000+RESERVE_SECONDS));self.eventually(self.stock,(7,0,7));got.append(self.stock())
        self.ok(f'/api/orders/{a}/refund',{});self.eventually(self.stock,(10,0,10));got.append(self.stock())
        expected=[(10,0,10),(10,3,7),(7,0,7),(7,2,5),(7,0,7),(10,0,10)]
        self.assertEqual(got,expected);self.invariants();self.record('1. Цепочка остатков',expected,got)

    def run_contention(self,stock,buyers,label):
        results=[]
        for iteration in range(ROUNDS):
            self.fresh();self.receive(stock)
            calls=[lambda uid=uid:self.checkout(uid,port=self.ports[uid%2]) for uid in range(1,buyers+1)]
            replies=self.parallel(calls);codes=[code for code,_ in replies]
            self.assertEqual(codes.count(200),min(stock,buyers),replies)
            self.assertEqual(codes.count(409),max(0,buyers-stock),replies)
            self.assertEqual(self.scalar('SELECT count(*) FROM orders'),min(stock,buyers))
            self.assertEqual(self.stock(),(stock,min(stock,buyers),max(0,stock-buyers)))
            self.invariants();results.append(dict(round=iteration+1,success=codes.count(200),shortage=codes.count(409)))
        self.record(label,dict(success=min(stock,buyers),shortage=max(0,buyers-stock),rounds=ROUNDS),results)

    def test_02_last_one_twenty(self):self.run_contention(1,20,'2. Одна единица / 20 покупателей')
    def test_03_ten_fifty(self):self.run_contention(10,50,'3. Десять единиц / 50 покупателей')
    def test_03b_configurable(self):self.run_contention(CUSTOM_STOCK,CUSTOM_BUYERS,'Параметризуемая конкуренция')

    def test_04_multiline_rollback(self):
        self.receive(3)
        code,r=self.checkout(items=[dict(product_id=1,qty=1),dict(product_id=2,qty=1)])
        self.assertEqual(code,409,r);self.assertEqual(self.stock(),(3,0,3));self.assertEqual(self.scalar('SELECT count(*) FROM orders'),0)
        self.assertEqual(self.scalar('SELECT count(*) FROM reservations'),0);self.invariants();self.record('4. Откат всего заказа','409, A=3/0/3, заказов 0',r)

    def test_05_idempotency(self):
        self.receive(5)
        replies=self.parallel([lambda i=i:self.checkout(1,key='one-identical-key',port=self.ports[i%2]) for i in range(20)])
        self.assertTrue(all(code==200 for code,_ in replies),replies)
        self.assertEqual(len({r['id'] for _,r in replies}),1)
        code,r=self.checkout(1,qty=2,key='one-identical-key');self.assertEqual(code,409,r)
        self.sql('UPDATE products SET price=1300 WHERE id=1')
        self.assertEqual(self.checkout(1,key='one-identical-key')[0],200)
        self.assertEqual(self.scalar('SELECT count(*) FROM orders'),1);self.assertEqual(self.stock(),(5,1,4));self.invariants()
        self.record('5. Повторы и другой состав','1 заказ; конфликт 409; смена цены не меняет повтор',replies)

    def test_06_payment_duplicates(self):
        self.receive(1);o=self.order();p=self.payment(o)
        for _ in range(5):self.paid(p)
        self.ok(f'/api/shop/payments/{p}/simulate',{'result':'canceled'},1)
        self.assertEqual(self.scalar('SELECT status FROM payments'),'succeeded');self.assertEqual(self.stock(),(0,0,0))
        self.assertEqual(self.summary()['receipts'],120000);self.assertEqual(self.scalar("SELECT count(*) FROM stock_journal WHERE kind='sale'"),1)
        self.invariants();self.record('6. Пять оплат и поздний canceled','1 продажа, 120000 коп.',self.summary())

    def test_07_cancel_and_expire(self):
        self.receive(2);o=self.order();self.order(2)
        self.ok(f'/api/shop/orders/{o}/cancel',{},1);self.ok(f'/api/shop/orders/{o}/cancel',{},1)
        self.clock_at((2000000000+RESERVE_SECONDS));self.eventually(self.stock,(2,0,2));self.assertEqual(self.summary()['receipts'],0)
        self.assertEqual(self.scalar("SELECT count(*) FROM stock_journal WHERE kind='release'"),2)
        self.invariants();self.record('7. Повтор отмены и истечение','2 освобождения, поступлений денег нет',self.stock())

    def test_08_races_and_boundary(self):
        outcomes=[]
        for delta in (-.001,0,.001):
            self.fresh();self.receive(1);o=self.order();p=self.payment(o);self.clock_at((2000000000+RESERVE_SECONDS)+delta)
            self.paid(p)
            accepted=self.scalar('SELECT sale_accepted FROM orders')
            self.assertEqual(accepted,int(delta<0));self.invariants();outcomes.append(dict(now=(2000000000+RESERVE_SECONDS)+delta,sale_accepted=accepted))
        for round in range(ROUNDS):
            self.fresh();self.receive(1);o=self.order();p=self.payment(o)
            replies=self.parallel([lambda:self.call(f'/api/shop/payments/{p}/simulate',{'result':'succeeded'},1,self.ports[0]),lambda:self.call(f'/api/shop/orders/{o}/cancel',{},1,self.ports[1])])
            self.assertEqual(replies[0][0],200,replies)
            accepted=self.scalar('SELECT sale_accepted FROM orders')
            if accepted:
                self.assertEqual(replies[1][0],409);self.assertEqual(self.stock(),(0,0,0));winner='payment → consumed'
            else:
                self.assertEqual(replies[1][0],200);self.eventually(lambda:self.scalar('SELECT status FROM payments'),'refunded');self.assertEqual(self.stock(),(1,0,1));winner='cancel → released → payment_conflict → refund'
            self.invariants();outcomes.append(dict(round=round+1,winner=winner,server_time=2000000000,expires_at=(2000000000+RESERVE_SECONDS)))
        for round in range(ROUNDS):
            self.fresh();self.receive(1);o=self.order();p=self.payment(o);self.clock_at((2000000000+RESERVE_SECONDS))
            replies=self.parallel([lambda:self.call(f'/api/shop/payments/{p}/simulate',{'result':'succeeded'},1,self.ports[0]),lambda:self.call('/api/stock/sweep',{},111,self.ports[1])])
            self.assertEqual([code for code,_ in replies],[200,200],replies)
            self.eventually(lambda:self.scalar('SELECT status FROM payments'),'refunded')
            self.assertEqual(self.scalar('SELECT sale_accepted FROM orders'),0)
            self.assertEqual(self.stock(),(1,0,1));self.invariants()
            outcomes.append(dict(boundary_round=round+1,winner='expiry (now >= expires_at) → conflict → refund',server_time=(2000000000+RESERVE_SECONDS),expires_at=(2000000000+RESERVE_SECONDS)))
        self.record('8. Гонки и точная граница','До срока продажа, на/после — возврат; один победитель',outcomes)

    def test_09_late_payment(self):
        self.receive(1);a=self.order(1);pa=self.payment(a);self.ok(f'/api/shop/orders/{a}/cancel',{},1)
        b=self.order(2);pb=self.payment(b,2);self.paid(pb,2);self.paid(pa,1)
        self.eventually(lambda:self.scalar('SELECT status FROM payments WHERE id=?',(pa,)),'refunded')
        for _ in range(3):self.paid(pa,1);self.paid(pb,2)
        self.assertEqual(self.scalar('SELECT pay_status FROM orders WHERE id=?',(a,)),'payment_conflict')
        s=self.summary();self.assertEqual((s['receipts'],s['refunds'],s['cash_flow'],s['cogs_paid'],s['cogs_refunded'],s['result']),(240000,120000,120000,70000,0,50000))
        self.assertEqual(self.stock(),(0,0,0));self.invariants();self.record('9. Поздняя оплата A после продажи Б','Поток 1200 ₽, себестоимость 700 ₽, результат 500 ₽',s)

    def test_10_restart_expiry(self):
        self.receive(1);self.order();self.stop();self.clock_at((2000000000+RESERVE_SECONDS));self.boot()
        self.eventually(self.stock,(1,0,1),seconds=.4);self.boot();self.assertEqual(self.stock(),(1,0,1))
        self.assertEqual(self.scalar("SELECT count(*) FROM stock_journal WHERE kind='release'"),1);self.invariants();self.record('10. Перезапуск с просроченным резервом','1 освобождение до двух интервалов',self.stock())

    def test_11_restart_refund_error_lost_response(self):
        self.receive(1);o=self.order();p=self.payment(o);self.ok(f'/api/shop/orders/{o}/cancel',{},1)
        self.sql('INSERT INTO simulator_faults VALUES(?,1,1)',(p,))
        self.boot('after_late_payment',both=False)
        try:self.paid(p)
        except (ConnectionError,URLError,__import__('http.client').client.RemoteDisconnected):pass
        self.processes[0].wait(timeout=5);self.assertEqual(self.processes[0].returncode,87)
        self.assertEqual(self.scalar("SELECT count(*) FROM refund_jobs WHERE state='refund_pending'"),1)
        self.assertEqual(self.scalar("SELECT count(*) FROM finance_operations WHERE type='receipt'"),1)
        self.boot(both=False)
        self.eventually(lambda:bool(self.scalar('SELECT last_error FROM refund_jobs')),True)
        self.clock_at(2000000001)
        self.eventually(lambda:self.scalar('SELECT attempts FROM refund_jobs')>=2,True)
        # Wait until the lost-response error is durably recorded before advancing.
        self.eventually(lambda:'потерян' in (self.scalar('SELECT last_error FROM refund_jobs') or ''),True)
        self.clock_at(2000000002)
        self.eventually(lambda:self.scalar('SELECT status FROM payments'),'refunded')
        self.assertGreaterEqual(self.scalar('SELECT attempts FROM refund_jobs'),3)
        self.assertEqual(self.scalar('SELECT count(*) FROM simulator_refunds'),1)
        self.assertEqual(self.scalar("SELECT count(*) FROM finance_operations WHERE type='refund'"),1)
        self.assertEqual(self.stock(),(1,0,1));self.invariants();self.record('11. Сбой, временная ошибка, потеря успешного ответа','Один возврат; >=3 попыток; товар не добавлен',self.sql('SELECT * FROM refund_jobs'))

    def test_12_crash_uncommitted(self):
        self.receive(1);self.boot('checkout_before_commit',both=False)
        try:self.checkout(1,key='crash-retry-key')
        except (ConnectionError,URLError,__import__('http.client').client.RemoteDisconnected):pass
        self.processes[0].wait(timeout=5);self.assertEqual(self.processes[0].returncode,86)
        self.assertEqual(self.scalar('SELECT count(*) FROM orders'),0);self.assertEqual(self.scalar('SELECT count(*) FROM reservations'),0)
        self.assertEqual(self.stock(),(1,0,1));self.boot();self.order(key='crash-retry-key')
        self.assertEqual(self.stock(),(1,1,0));self.invariants();self.record('12. Остановка до commit','Откат и успешный повтор, 1 заказ',self.stock())

    def test_13_security(self):
        self.receive(1);o=self.order();p=self.payment(o)
        before=self.stock();s=self.summary()
        self.assertEqual(self.call('/api/stock/receive',dict(product_id=1,qty=10,request_key='forbidden-key'),uid=2)[0],403)
        self.assertEqual(self.call(f'/api/shop/orders/{o}/cancel',{},2)[0],404)
        self.assertEqual(self.call(f'/api/shop/orders/{o}',uid=2)[0],404)
        self.assertEqual(self.call(f'/api/shop/payments/{p}/simulate',dict(result='succeeded'),2)[0],404)
        self.assertEqual(self.call(f'/api/shop/payments/{p}/simulate',dict(result='succeeded',amount_kop=1),1)[0],400)
        status,_=self.call('/api/shop/checkout',dict(items=[dict(product_id=1,qty=1)],name='X',delivery='Самовывоз',request_key='override-time',expires_at=9999999999),2)
        self.assertEqual(status,400)
        self.assertEqual(self.call('/api/stock',authorization=False)[0],401)
        req=Request(f'http://127.0.0.1:{self.ports[0]}/api/stock',headers={'Authorization':'tma fake'})
        with self.assertRaises(HTTPError) as cm:urlopen(req)
        self.assertEqual(cm.exception.code,401)
        self.assertEqual(self.stock(),before);self.assertEqual(self.summary(),s);self.invariants();self.record('13. Покупатель, чужой резерв, цена, срок','403 / 404 / 400 / 401, данные неизменны','Соответствует')

    def test_14_recipe_snapshots(self):
        self.receive(2)
        code,r=self.checkout(items=[dict(product_id=1,qty=1,add=['jalapeno','cheese'],remove=['Лук']),dict(product_id=1,qty=1)])
        self.assertEqual(code,200,r);oid=r['id'];p=self.payment(oid)
        self.sql('UPDATE products SET price=900,cost_kop=1 WHERE id=1');self.paid(p)
        self.assertEqual(self.scalar('SELECT total FROM orders'),2470)
        self.assertEqual(self.summary()['cogs_paid'],142800)
        self.assertEqual(self.scalar('SELECT count(*) FROM reservation_items'),1)
        self.invariants();self.record('Дополнительно: рецепты и снимки','2470 ₽ / себестоимость 1428 ₽ / общий резерв 2',self.sql('SELECT * FROM order_items'))

    def test_15_confirmed_refund_only(self):
        self.receive(1);oid=self.order();pid=self.payment(oid);self.paid(pid)
        self.sql('INSERT INTO simulator_faults VALUES(?,100,0)',(pid,))
        self.ok(f'/api/orders/{oid}/refund',{})
        self.eventually(lambda:bool(self.scalar('SELECT last_error FROM refund_jobs')),True)
        self.assertEqual(self.stock(),(0,0,0));self.assertEqual(self.summary()['refunds'],0)
        self.sql('UPDATE simulator_faults SET errors=0 WHERE payment_id=?',(pid,))
        self.clock_at(2000000001)
        self.eventually(lambda:self.scalar('SELECT status FROM payments'),'refunded')
        for _ in range(3):self.ok(f'/api/orders/{oid}/refund',{})
        self.assertEqual(self.stock(),(1,0,1));self.assertEqual(self.summary()['cogs_refunded'],70000)
        self.invariants();self.record('Дополнительно: обычный возврат','До подтверждения ничего не меняется, после +1 товар / -700 ₽ себестоимости',self.summary())
