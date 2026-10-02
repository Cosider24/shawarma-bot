"""Работа с базой данных SQLite. Все цены и суммы считаются здесь, по данным из БД."""
import sqlite3
from datetime import datetime

DB_PATH = "shop.db"

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


def get_conn():
    # isolation_level=None: транзакциями управляем вручную (BEGIN / COMMIT)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
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
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO products(name, description, price) VALUES (?, ?, ?)", SEED_PRODUCTS
        )
    conn.close()


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
def create_order(user_id, customer_name, delivery):
    """Одна транзакция: читаем корзину, создаём заказ и его состав, очищаем корзину.
    Если корзина пуста (в т.ч. при повторном нажатии «Подтвердить») — возвращаем None."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        items = conn.execute(
            "SELECT p.name, p.price, c.qty FROM cart_items c "
            "JOIN products p ON p.id = c.product_id WHERE c.user_id=? AND c.qty > 0",
            (user_id,),
        ).fetchall()
        if not items:
            conn.execute("ROLLBACK")
            return None
        total = sum(i["price"] * i["qty"] for i in items)
        cur = conn.execute(
            "INSERT INTO orders(user_id, customer_name, delivery, payment, total, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                customer_name,
                delivery,
                "При получении",
                total,
                STATUSES[0],
                datetime.now().strftime("%d.%m.%Y %H:%M"),
            ),
        )
        order_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO order_items(order_id, product_name, price, qty) VALUES (?, ?, ?, ?)",
            [(order_id, i["name"], i["price"], i["qty"]) for i in items],
        )
        conn.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
        conn.execute("COMMIT")
        return order_id
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


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


def set_status(order_id, status):
    conn = get_conn()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.close()
