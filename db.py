"""Работа с базой данных SQLite. Все цены и суммы считаются здесь, по данным из БД."""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.getenv('SHOP_DB', 'shop.db')

STATUSES = ["Новый", "Готовится", "Готов к выдаче", "Выдан", "Отменён"]
MAX_QTY = 20

SEED_PRODUCTS = [
    ("Шаурма классическая", "Курица, свежие овощи, фирменный соус, лаваш.", 250),
    ("Шаурма с говядиной", "Сочная говядина, маринованный лук, томаты, чесночный соус.", 290),
    ("Шаурма сырная", "Курица, плавленый сыр, огурцы, сливочный соус.", 270),
    ("Шаурма острая", "Курица, халапеньо, острый соус, овощи.", 280),
    ("Шаурма вегетарианская", "Фалафель, хумус, овощи, тахини.", 220),
    ("Морс домашний 0.5 л", "Клюквенный морс, охлаждённый.", 90),
]


# Учебная себестоимость (руб.) по названию товара
SEED_COSTS_RUB = {
    "Шаурма классическая": 110,
    "Шаурма с говядиной": 150,
    "Шаурма сырная": 120,
    "Шаурма острая": 115,
    "Шаурма вегетарианская": 95,
    "Морс домашний 0.5 л": 35,
}


def get_conn():
    # isolation_level=None: транзакциями управляем вручную (BEGIN / COMMIT)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,          -- Telegram ID
            platform TEXT NOT NULL DEFAULT 'telegram',
            name TEXT
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            price INTEGER NOT NULL CHECK (price > 0),
            available INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS cart_items (
            user_id INTEGER NOT NULL REFERENCES users(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            qty INTEGER NOT NULL CHECK (qty > 0),
            PRIMARY KEY (user_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            customer_name TEXT NOT NULL,
            delivery TEXT NOT NULL,
            payment TEXT NOT NULL,
            total INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            product_name TEXT NOT NULL,      -- название на момент покупки
            price INTEGER NOT NULL,          -- цена на момент покупки
            qty INTEGER NOT NULL
        );
        """
    )
    migrate(conn)
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO products(name, description, price) VALUES (?, ?, ?)", SEED_PRODUCTS
        )
    for name, cost in SEED_COSTS_RUB.items():  # проставить себестоимость там, где её ещё нет
        conn.execute(
            "UPDATE products SET cost_kop=? WHERE name=? AND cost_kop=0", (cost * 100, name)
        )
    import stock
    stock.migrate(conn)
    conn.close()


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    """Расширение схемы магазина под финансы. Безопасно запускается повторно."""
    if "cost_kop" not in _columns(conn, "products"):
        conn.execute("ALTER TABLE products ADD COLUMN cost_kop INTEGER NOT NULL DEFAULT 0")
    if "cost_kop" not in _columns(conn, "order_items"):
        conn.execute("ALTER TABLE order_items ADD COLUMN cost_kop INTEGER NOT NULL DEFAULT 0")
        # cost_source: 'order' — себестоимость сохранена при заказе;
        # 'restored' — заказ создан до финансов, себестоимость восстановлена по каталогу
        conn.execute("ALTER TABLE order_items ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'order'")
        for name, cost in SEED_COSTS_RUB.items():
            conn.execute(
                "UPDATE order_items SET cost_kop=?, cost_source='restored' WHERE product_name=?",
                (cost * 100, name),
            )
    if "pay_status" not in _columns(conn, "orders"):
        conn.execute(
            "ALTER TABLE orders ADD COLUMN pay_status TEXT NOT NULL DEFAULT 'awaiting_payment'"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            provider TEXT NOT NULL,                 -- 'yookassa' | 'simulator'
            provider_payment_id TEXT,               -- provider_payment_charge_id / sim-N
            tg_charge_id TEXT,                      -- telegram_payment_charge_id
            amount_kop INTEGER NOT NULL CHECK (amount_kop > 0),
            currency TEXT NOT NULL DEFAULT 'RUB',
            status TEXT NOT NULL CHECK (status IN
                ('pending','succeeded','canceled','refund_pending','refunded')),
            refund_id TEXT,
            refund_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_provider_id
            ON payments(provider_payment_id) WHERE provider_payment_id IS NOT NULL;
        -- у заказа не может быть двух «живых» оплат одновременно
        CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_one_paid
            ON payments(order_id) WHERE status IN ('succeeded','refund_pending','refunded');

        CREATE TABLE IF NOT EXISTS finance_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('receipt','refund','expense')),
            amount_kop INTEGER NOT NULL CHECK (amount_kop > 0),
            occurred_at TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS', время сервера
            order_id INTEGER REFERENCES orders(id),
            payment_id INTEGER REFERENCES payments(id),
            external_id TEXT UNIQUE,                -- защита от повторного события
            category TEXT,
            description TEXT,
            author_id INTEGER,                      -- 0 = система (платёж)
            voided_at TEXT,                         -- расход отменён (запись остаётся)
            voided_by INTEGER,
            replaces_id INTEGER REFERENCES finance_operations(id),
            created_at TEXT NOT NULL
        );
        -- одно поступление и один возврат на заказ
        CREATE UNIQUE INDEX IF NOT EXISTS ux_fin_order_type
            ON finance_operations(order_id, type) WHERE type IN ('receipt','refund');
        CREATE INDEX IF NOT EXISTS ix_fin_time ON finance_operations(occurred_at);
        """
    )


def register_user(user_id, name):
    conn = get_conn()
    conn.execute(
        "INSERT INTO users(id, name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
        (user_id, name),
    )
    conn.close()


# ---------- каталог ----------
def get_products():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM products WHERE available=1 ORDER BY id").fetchall()
    conn.close()
    return rows


def get_product(product_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    conn.close()
    return row


# ---------- корзина ----------
def get_cart(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT p.id AS product_id, p.name, p.price, c.qty "
        "FROM cart_items c JOIN products p ON p.id = c.product_id "
        "WHERE c.user_id=? ORDER BY p.id",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def cart_total(rows):
    return sum(r["price"] * r["qty"] for r in rows)


def change_qty(user_id, product_id, delta):
    """Изменить количество на delta. Если стало <= 0 — позиция удаляется.
    Выше MAX_QTY не пускаем. Возвращает новое количество."""
    conn = get_conn()
    try:
        product = conn.execute(
            "SELECT available FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if product is None or not product["available"]:
            return 0
        row = conn.execute(
            "SELECT qty FROM cart_items WHERE user_id=? AND product_id=?",
            (user_id, product_id),
        ).fetchone()
        new_qty = min((row["qty"] if row else 0) + delta, MAX_QTY)
        if new_qty <= 0:
            conn.execute(
                "DELETE FROM cart_items WHERE user_id=? AND product_id=?",
                (user_id, product_id),
            )
            return 0
        conn.execute(
            "INSERT INTO cart_items(user_id, product_id, qty) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, product_id) DO UPDATE SET qty=excluded.qty",
            (user_id, product_id, new_qty),
        )
        return new_qty
    finally:
        conn.close()


def remove_item(user_id, product_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM cart_items WHERE user_id=? AND product_id=?", (user_id, product_id)
    )
    conn.close()


# ---------- заказы ----------
def create_order(user_id,customer_name,delivery,request_key=None):
    import stock
    import uuid
    rows=get_cart(user_id)
    if not rows:
        return None
    return stock.checkout(user_id,[dict(product_id=r['product_id'],qty=r['qty']) for r in rows],
                          customer_name,delivery,request_key or str(uuid.uuid4()))['id']


def get_order(order_id):
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)
    ).fetchall()
    conn.close()
    return order, items


def get_user_orders(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_new_orders():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status=? ORDER BY id", (STATUSES[0],)
    ).fetchall()
    conn.close()
    return rows


def set_status(order_id,status):
    import stock
    import finance
    if status not in STATUSES:
        raise finance.FinanceError('Неизвестный статус')
    if status == 'Отменён':
        return stock.cancel(order_id,0,manager=True)
    with finance.tx() as conn:
        o=conn.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if not o or o['pay_status'] != 'paid':
            raise finance.FinanceError('Готовить и выдавать можно только оплаченные заказы')
        if STATUSES.index(status) < STATUSES.index(o['status']):
            raise finance.FinanceError('Нельзя вернуть предыдущий этап приготовления')
        conn.execute('UPDATE orders SET status=? WHERE id=?',(status,order_id))
