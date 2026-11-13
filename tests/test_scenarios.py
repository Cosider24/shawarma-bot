"""Автотесты сценариев раздела 8. Запуск: python -m unittest -v
Используют временную БД и фиктивный токен — .env и боевая shop.db не затрагиваются."""
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import date
from urllib.parse import urlencode

os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN_NOT_REAL"
os.environ["MANAGER_IDS"] = "111"

import config  # noqa: E402
import db  # noqa: E402
import finance  # noqa: E402
import server  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

TOKEN = "123456:TEST_TOKEN_NOT_REAL"
TODAY = date.today().isoformat()


def make_init_data(user_id, token=TOKEN, age=0):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAA",
              "user": json.dumps({"id": user_id, "first_name": "T"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        db.init_db()
        import stock
        for p in stock.catalog():
            stock.receive(p['id'],100,'test',f'test-initial-{p["id"]}')
        config.PAYMENT_MODE = "simulator"
        config.BOT_TOKEN, config.MANAGER_IDS = TOKEN, {111}

    def tearDown(self):
        self.tmp.cleanup()

    def make_order(self, user_id=1, qty=4):
        """Заказ через штатный путь: корзина → create_order. 4 × 300? берём 1-й товар (250 ₽)."""
        db.register_user(user_id, "Buyer")
        db.change_qty(user_id, 1, qty)
        return db.create_order(user_id, "Покупатель", "Самовывоз")

    def summary(self):
        return finance.summary("2000-01-01", TODAY)

    def pay(self, oid, user_id=1):
        p = finance.create_payment(oid, user_id, "simulator")
        return p, finance.apply_payment_success(p["id"], p["amount_kop"], "RUB", f"sim-{p['id']}")


class TestPayments(Base):
    def test_pending_not_in_summary(self):
        oid = self.make_order()
        finance.create_payment(oid, 1, "simulator")
        s = self.summary()
        self.assertEqual((s["receipts"], s["result"]), (0, 0))
        self.assertEqual(db.get_order(oid)[0]["pay_status"], "awaiting_payment")

    def test_success_once_and_duplicate(self):
        oid = self.make_order()  # 4 × 250 = 1000 ₽, себестоимость 4 × 110 = 440 ₽
        p, r = self.pay(oid)
        self.assertFalse(r["duplicate"])
        again = finance.apply_payment_success(p["id"], p["amount_kop"], "RUB", f"sim-{p['id']}")
        self.assertTrue(again["duplicate"])
        s = self.summary()
        self.assertEqual((s["receipts"], s["receipts_count"]), (100000, 1))
        self.assertEqual(s["cogs_paid"], 44000)
        self.assertEqual(s["result"], 100000 - 44000)

    def test_canceled_payment_then_trusted_success(self):
        oid = self.make_order()
        p = finance.create_payment(oid, 1, "simulator")
        self.assertTrue(finance.cancel_payment(p["id"]))
        self.assertEqual(self.summary()["receipts"], 0)
        finance.apply_payment_success(p['id'],p['amount_kop'],'RUB','x')
        self.assertEqual(self.summary()['receipts'],p['amount_kop'])

    def test_amount_and_currency_mismatch_rejected(self):
        oid = self.make_order()
        p = finance.create_payment(oid, 1, "simulator")
        with self.assertRaises(finance.FinanceError):
            finance.apply_payment_success(p["id"], 100, "RUB", "x")
        with self.assertRaises(finance.FinanceError):
            finance.apply_payment_success(p["id"], p["amount_kop"], "USD", "x")
        self.assertIsNotNone(finance.check_precheckout(f"pay:{p['id']}", 100, "RUB", 1))
        self.assertIsNone(finance.check_precheckout(f"pay:{p['id']}", p["amount_kop"], "RUB", 1))
        self.assertEqual(self.summary()["receipts"], 0)

    def test_only_owner_can_pay_and_invoice_retry_is_same(self):
        oid = self.make_order()
        with self.assertRaises(finance.FinanceError):
            finance.create_payment(oid, 2, "simulator")
        old = finance.create_payment(oid, 1, "simulator")
        new = finance.create_payment(oid, 1, "simulator")
        self.assertEqual(old['id'],new['id'])
        self.assertEqual(finance.get_payment(old["id"])["status"], "pending")
        self.assertIsNone(finance.check_precheckout(f"pay:{old['id']}", old["amount_kop"], "RUB", 1))
        self.assertIsNone(finance.check_precheckout(f"pay:{new['id']}", new["amount_kop"], "RUB", 1))

    def test_cost_snapshot_survives_catalog_change(self):
        oid = self.make_order(qty=1)
        conn = db.get_conn()
        conn.execute("UPDATE products SET cost_kop=99900, price=999 WHERE id=1")
        conn.close()
        self.pay(oid)
        s = self.summary()
        self.assertEqual((s["receipts"], s["cogs_paid"]), (25000, 11000))


class TestRefundAndExpenses(Base):
    def test_full_cycle_matches_manual_calculation(self):
        oid = self.make_order()          # 1000 ₽, себестоимость 440 ₽
        self.pay(oid)
        finance.add_expense(111, "Доставка", "100", TODAY, "Курьер")
        s = self.summary()
        self.assertEqual(s["cash_flow"], 100000 - 10000)                 # 900 ₽
        self.assertEqual(s["result"], 100000 - 44000 - 10000)            # 460 ₽
        # запуск возврата ещё НЕ меняет сводку
        p = finance.request_refund(oid)
        self.assertEqual(self.summary()["cash_flow"], 90000)
        self.assertEqual(finance.request_refund(oid)['id'],p['id'])  # повтор безопасен
        # неуспешный возврат: сводка прежняя, повторить можно
        finance.fail_refund(p["id"], "отказ")
        self.assertEqual(self.summary()["cash_flow"], 90000)
        finance.request_refund(oid)
        finance.complete_refund(p["id"], "r1")
        self.assertTrue(finance.complete_refund(p["id"])["duplicate"])
        s = self.summary()
        self.assertEqual(s["refunds_count"], 1)
        self.assertEqual(s["cash_flow"], 100000 - 100000 - 10000)        # −100 ₽
        self.assertEqual(s["result"], 100000 - 100000 - 44000 + 44000 - 10000)  # −100 ₽
        self.assertEqual(finance.request_refund(oid)['status'],'refunded')

    def test_refund_requires_payment(self):
        oid = self.make_order()
        with self.assertRaises(finance.FinanceError):
            finance.request_refund(oid)

    def test_expense_validation(self):
        for bad in ("0", "-5", "abc", "", "1.234", "99999999999"):
            with self.assertRaises(finance.FinanceError, msg=bad):
                finance.add_expense(111, "Доставка", bad, TODAY, "x")
        with self.assertRaises(finance.FinanceError):
            finance.add_expense(111, "Доставка", "10", "2999-01-01", "x")
        with self.assertRaises(finance.FinanceError):
            finance.add_expense(111, "Нет такой", "10", TODAY, "x")
        self.assertEqual(self.summary()["expenses"], 0)

    def test_expense_double_submit_void_and_edit_keep_history(self):
        a = finance.add_expense(111, "Реклама", "50,50", TODAY, "Листовки", "req-1")
        b = finance.add_expense(111, "Реклама", "50,50", TODAY, "Листовки", "req-1")
        self.assertTrue(b["duplicate"])
        self.assertEqual(self.summary()["expenses"], 5050)
        n = finance.edit_expense(a["id"], 111, "Реклама", "70", TODAY, "Листовки+")
        self.assertEqual(self.summary()["expenses"], 7000)
        finance.void_expense(n["id"], 111)
        self.assertEqual(self.summary()["expenses"], 0)
        self.assertEqual(len(finance.journal("2000-01-01", TODAY)), 2)  # обе записи остались
        with self.assertRaises(finance.FinanceError):
            finance.void_expense(n["id"], 111)

    def test_old_period_refund_makes_result_negative(self):
        oid = self.make_order()
        p, _ = self.pay(oid)
        conn = db.get_conn()  # «вчерашняя» оплата
        conn.execute("UPDATE finance_operations SET occurred_at='2020-01-10 10:00:00' WHERE type='receipt'")
        conn.close()
        finance.request_refund(oid)
        finance.complete_refund(p["id"])
        today = finance.summary(TODAY, TODAY)
        self.assertEqual(today["cash_flow"], -100000)
        self.assertEqual(finance.summary("2020-01-01", "2020-01-31")["result"], 100000 - 44000)

    def test_unique_receipt_per_order_enforced_by_db(self):
        oid = self.make_order()
        self.pay(oid)
        conn = db.get_conn()
        with self.assertRaises(Exception):
            conn.execute("INSERT INTO finance_operations(type,amount_kop,occurred_at,order_id,created_at) "
                         "VALUES ('receipt',1,'2020-01-01 00:00:00',?, 'x')", (oid,))
        conn.close()


class TestApi(Base):
    async def _client(self):
        c = TestClient(TestServer(server.create_app(None)))
        await c.start_server()
        return c

    def run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_access_rules(self):
        async def go():
            c = await self._client()
            try:
                H = lambda d: {"Authorization": "tma " + d}
                self.assertEqual((await c.get("/api/summary")).status, 401)                       # нет авторизации
                self.assertEqual((await c.get("/api/summary", headers=H("junk"))).status, 401)    # мусор
                bad = make_init_data(111, token="1:OTHER_BOT_TOKEN")
                self.assertEqual((await c.get("/api/summary", headers=H(bad))).status, 401)       # чужая подпись
                old = make_init_data(111, age=999999)
                self.assertEqual((await c.get("/api/summary", headers=H(old))).status, 401)       # устарели
                buyer = make_init_data(222)
                self.assertEqual((await c.get("/api/summary", headers=H(buyer))).status, 403)     # не менеджер
                self.assertEqual((await c.get("/api/orders", headers=H(buyer))).status, 403)
                ok = await c.get("/api/summary", headers=H(make_init_data(111)))
                self.assertEqual(ok.status, 200)
                self.assertEqual((await c.get("/")).status, 200)                                  # страница без данных
            finally:
                await c.close()
        self.run_async(go())

    def test_expense_and_refund_via_api(self):
        oid = self.make_order()
        p, _ = self.pay(oid)

        async def go():
            c = await self._client()
            H = {"Authorization": "tma " + make_init_data(111)}
            try:
                r = await c.post("/api/expenses", json={"category": "Доставка", "amount": "0", "date": TODAY,
                                                         "description": "x"}, headers=H)
                self.assertEqual(r.status, 400)
                self.assertIn("больше нуля", (await r.json())["error"])
                r = await c.post("/api/expenses", json={"category": "Доставка", "amount": "100", "date": TODAY,
                                                         "description": "Курьер", "request_id": "a1"}, headers=H)
                self.assertEqual(r.status, 200)
                r = await c.post(f'/api/orders/{oid}/refund',json={},headers=H)
                self.assertEqual((await r.json())['status'],'refund_pending')
                r = await c.post(f'/api/orders/{oid}/refund',json={},headers=H)
                self.assertEqual(r.status,200)
                import worker
                await worker.process_refunds()
                s = await (await c.get(f'/api/summary?from=2000-01-01&to={TODAY}',headers=H)).json()
                self.assertEqual((s["refunds"], s["cash_flow"], s["result"]), (100000, -10000, -10000))
            finally:
                await c.close()
        self.run_async(go())


class TestMigration(unittest.TestCase):
    def test_old_database_is_upgraded(self):
        """Старая БД (без финансов) получает новые колонки; себестоимость старых заказов восстанавливается."""
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db.DB_PATH = os.path.join(d, "old.db")
            conn = sqlite3.connect(db.DB_PATH)
            conn.executescript("""
              CREATE TABLE users(id INTEGER PRIMARY KEY, platform TEXT NOT NULL DEFAULT 'telegram', name TEXT);
              CREATE TABLE products(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT NOT NULL,
                price INTEGER NOT NULL, available INTEGER NOT NULL DEFAULT 1);
              CREATE TABLE cart_items(user_id INTEGER, product_id INTEGER, qty INTEGER, PRIMARY KEY(user_id, product_id));
              CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, customer_name TEXT, delivery TEXT,
                payment TEXT, total INTEGER, status TEXT, created_at TEXT);
              CREATE TABLE order_items(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, product_name TEXT,
                price INTEGER, qty INTEGER);
              INSERT INTO products(name,description,price) VALUES('Шаурма классическая','x',250);
              INSERT INTO users(id,name) VALUES(1,'a');
              INSERT INTO orders(user_id,customer_name,delivery,payment,total,status,created_at)
                VALUES(1,'a','Самовывоз','При получении',500,'Новый','01.10.2026 10:00');
              INSERT INTO order_items(order_id,product_name,price,qty) VALUES(1,'Шаурма классическая',250,2);
            """)
            conn.close()
            db.init_db()
            db.init_db()  # повторный запуск безопасен
            o, items = db.get_order(1)
            self.assertEqual(o["pay_status"], "awaiting_payment")
            self.assertEqual((items[0]["cost_kop"], items[0]["cost_source"]), (11000, "restored"))


if __name__ == "__main__":
    unittest.main()
