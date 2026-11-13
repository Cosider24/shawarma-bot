"""Платежи и финансовый журнал. Вся логика денег здесь, на сервере.

Деньги — целые копейки. Суммы платежа берутся из сохранённого заказа, а не из запроса.
Каждое изменение статуса и запись операции — одна транзакция (BEGIN IMMEDIATE ... COMMIT).
"""
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import db

CURRENCY = "RUB"
EXPENSE_CATEGORIES = ["Доставка", "Реклама", "Аренда", "Упаковка", "Прочее"]
MAX_EXPENSE_KOP = 100_000_000  # 1 000 000 ₽ — защита от опечаток
PAY_LABELS = {"awaiting_payment": "Ожидает оплаты", "paid": "Оплачен", "refunded": "Возвращён", "expired":"Резерв истёк", "canceled":"Отменён", "payment_conflict":"Резерв закончился, оформляется возврат"}
PAYMENT_LABELS = {
    "pending": "Ожидает оплаты", "succeeded": "Успешен", "canceled": "Отменён",
    "refund_pending": "Возврат в обработке", "refunded": "Возвращён",
}


class FinanceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_money(kop: int) -> str:
    sign = "−" if kop < 0 else ""
    rub, k = divmod(abs(kop), 100)
    s = f"{rub:,}".replace(",", " ")
    return f"{sign}{s} ₽" if k == 0 else f"{sign}{s},{k:02d} ₽"


@contextmanager
def tx():
    """Транзакция: BEGIN IMMEDIATE → COMMIT, при любой ошибке ROLLBACK."""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- платежи
def create_payment(order_id,user_id,provider):
    import stock
    return stock.create_payment(order_id,user_id,provider)


def get_payment(payment_id):
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT p.*, o.user_id AS buyer_id FROM payments p JOIN orders o ON o.id=p.order_id WHERE p.id=?",
            (payment_id,),
        ).fetchone()
    finally:
        conn.close()


def parse_payload(payload: str):
    try:
        kind, pid = payload.split(":")
        if kind != "pay":
            raise ValueError
        return int(pid)
    except (ValueError, AttributeError):
        raise FinanceError("Неверный платёжный идентификатор")


def check_precheckout(payload, total_amount, currency, user_id):
    """Проверка перед списанием (pre_checkout_query). Возвращает None или текст ошибки."""
    try:
        p = get_payment(parse_payload(payload))
        if p is None:
            return "Счёт не найден. Создайте оплату заново."
        if p["buyer_id"] != user_id:
            return "Этот счёт выставлен другому покупателю."
        if p["status"] != "pending":
            return "Счёт устарел или уже оплачен."
        conn = db.get_conn()
        try:
            o = conn.execute("SELECT total, pay_status, status FROM orders WHERE id=?", (p["order_id"],)).fetchone()
        finally:
            conn.close()
        import stock
        c = db.get_conn()
        try:
            r = c.execute('SELECT * FROM reservations WHERE order_id=?',(p['order_id'],)).fetchone()
        finally:
            c.close()
        if r and (r['state'] != 'active' or stock.now() >= r['expires_at']):
            return 'Резерв закончился. Оформите новый заказ.'
        if o["pay_status"] != "awaiting_payment" or o["status"] == "Отменён":
            return "Заказ уже оплачен или отменён."
        if total_amount != p["amount_kop"] or total_amount != o["total"] * 100 or currency != p["currency"]:
            return "Сумма или валюта счёта не совпадает с заказом."
        return None
    except FinanceError as e:
        return str(e)


def apply_payment_success(payment_id,amount_kop,currency,provider_payment_id=None,tg_charge_id=None):
    import stock
    return stock.payment_success(payment_id,amount_kop,currency,provider_payment_id,tg_charge_id)


def cancel_payment(payment_id):
    """pending → canceled. Поступление не создаётся."""
    with tx() as c:
        cur = c.execute(
            "UPDATE payments SET status='canceled', updated_at=? WHERE id=? AND status='pending'",
            (now_iso(), payment_id),
        )
        return cur.rowcount == 1


# ---------------------------------------------------------------- возвраты
def request_refund(order_id):
    import stock
    return stock.request_refund(order_id)


def set_refund_id(payment_id, refund_id):
    with tx() as c:
        c.execute("UPDATE payments SET refund_id=? WHERE id=?", (refund_id, payment_id))


def complete_refund(payment_id,refund_id=None):
    import stock
    return stock.complete_refund(payment_id,refund_id)


def fail_refund(payment_id,error):
    import stock
    stock.refund_error(payment_id,error)


def _parse_amount(raw) -> int:
    try:
        d = Decimal(str(raw).replace(",", ".").replace(" ", "").strip())
    except (InvalidOperation, ValueError):
        raise FinanceError("Сумма расхода должна быть числом")
    if not d.is_finite():
        raise FinanceError("Сумма расхода должна быть числом")
    if d <= 0:
        raise FinanceError("Сумма расхода должна быть больше нуля")
    if d != d.quantize(Decimal("0.01")):
        raise FinanceError("Не больше двух знаков после запятой")
    kop = int(d * 100)
    if kop > MAX_EXPENSE_KOP:
        raise FinanceError("Слишком большая сумма расхода")
    return kop


def _validate_expense(category, amount, day, description):
    if category not in EXPENSE_CATEGORIES:
        raise FinanceError("Выберите категорию из списка")
    kop = _parse_amount(amount)
    try:
        d = date.fromisoformat(str(day))
    except ValueError:
        raise FinanceError("Дата должна быть в формате ГГГГ-ММ-ДД")
    if d > date.today():
        raise FinanceError("Дата расхода не может быть в будущем")
    description = (description or "").strip()
    if not description or len(description) > 200:
        raise FinanceError("Добавьте описание (до 200 символов)")
    when = now_iso() if d == date.today() else f"{d.isoformat()} 12:00:00"
    return kop, when, description


def add_expense(author_id, category, amount, day, description, request_id=None):
    kop, when, description = _validate_expense(category, amount, day, description)
    ext = f"expense:{request_id}" if request_id else None
    try:
        with tx() as c:
            cur = c.execute(
                "INSERT INTO finance_operations(type, amount_kop, occurred_at, external_id, category, description, "
                "author_id, created_at) VALUES ('expense', ?, ?, ?, ?, ?, ?, ?)",
                (kop, when, ext, category, description, author_id, now_iso()),
            )
            return {"id": cur.lastrowid, "duplicate": False}
    except sqlite3.IntegrityError:
        # двойное нажатие: тот же request_id уже записан
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT id FROM finance_operations WHERE external_id=?", (ext,)).fetchone()
        finally:
            conn.close()
        return {"id": row["id"], "duplicate": True}


def void_expense(op_id, author_id):
    """Отмена расхода: запись остаётся в журнале с отметкой."""
    with tx() as c:
        cur = c.execute(
            "UPDATE finance_operations SET voided_at=?, voided_by=? "
            "WHERE id=? AND type='expense' AND voided_at IS NULL",
            (now_iso(), author_id, op_id),
        )
        if cur.rowcount != 1:
            raise FinanceError("Расход не найден или уже отменён", 404)


def edit_expense(op_id, author_id, category, amount, day, description):
    """Исправление = отмена старой записи + новая запись со ссылкой replaces_id (история сохраняется)."""
    kop, when, description = _validate_expense(category, amount, day, description)
    with tx() as c:
        t = now_iso()
        cur = c.execute(
            "UPDATE finance_operations SET voided_at=?, voided_by=? "
            "WHERE id=? AND type='expense' AND voided_at IS NULL",
            (t, author_id, op_id),
        )
        if cur.rowcount != 1:
            raise FinanceError("Расход не найден или уже отменён", 404)
        cur = c.execute(
            "INSERT INTO finance_operations(type, amount_kop, occurred_at, category, description, author_id, "
            "replaces_id, created_at) VALUES ('expense', ?, ?, ?, ?, ?, ?, ?)",
            (kop, when, category, description, author_id, op_id, t),
        )
        return {"id": cur.lastrowid}


# ---------------------------------------------------------------- отчёты
def period_bounds(date_from: str, date_to: str):
    """Включительно по датам → [from 00:00:00, следующий день 00:00:00)."""
    try:
        a, b = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        raise FinanceError("Неверный формат даты периода")
    if a > b:
        raise FinanceError("Начало периода позже конца")
    return f"{a.isoformat()} 00:00:00", f"{(b + timedelta(days=1)).isoformat()} 00:00:00"


def summary(date_from, date_to):
    lo, hi = period_bounds(date_from, date_to)
    conn = db.get_conn()
    try:
        def total(op_type, extra=""):
            r = conn.execute(
                f"SELECT COALESCE(SUM(amount_kop),0) s, COUNT(*) n FROM finance_operations "
                f"WHERE type=? AND occurred_at>=? AND occurred_at<? {extra}", (op_type, lo, hi)).fetchone()
            return r["s"], r["n"]

        def cogs(op_type):
            return conn.execute(
                "SELECT COALESCE(SUM(oi.cost_kop*oi.qty),0) FROM order_items oi "
                "JOIN orders o ON o.id=oi.order_id AND o.sale_accepted=1 JOIN finance_operations f ON f.order_id=oi.order_id AND f.type=? "
                "AND f.occurred_at>=? AND f.occurred_at<?", (op_type, lo, hi)).fetchone()[0]

        receipts, n_rec = total("receipt")
        refunds, n_ref = total("refund")
        expenses, n_exp = total("expense", "AND voided_at IS NULL")
        cogs_paid, cogs_back = cogs("receipt"), cogs("refund")
        restored = conn.execute(
            "SELECT COUNT(DISTINCT oi.order_id) FROM order_items oi JOIN finance_operations f "
            "ON f.order_id=oi.order_id AND f.type='receipt' AND f.occurred_at>=? AND f.occurred_at<? "
            "WHERE oi.cost_source='restored'", (lo, hi)).fetchone()[0]
    finally:
        conn.close()
    return {
        "from": date_from, "to": date_to,
        "receipts": receipts, "receipts_count": n_rec,
        "refunds": refunds, "refunds_count": n_ref,
        "expenses": expenses, "expenses_count": n_exp,
        "cogs_paid": cogs_paid, "cogs_refunded": cogs_back,
        "cash_flow": receipts - refunds - expenses,
        "result": receipts - refunds - cogs_paid + cogs_back - expenses,
        "restored_cost_orders": restored,
    }


def journal(date_from, date_to):
    lo, hi = period_bounds(date_from, date_to)
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM finance_operations WHERE occurred_at>=? AND occurred_at<? "
            "ORDER BY occurred_at DESC, id DESC", (lo, hi)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_orders(limit=100):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, customer_name, total, status, pay_status, created_at FROM orders "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r, pay_label=PAY_LABELS.get(r["pay_status"], r["pay_status"])) for r in rows]
    finally:
        conn.close()


def order_card(order_id):
    conn = db.get_conn()
    try:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if o is None:
            raise FinanceError("Заказ не найден", 404)
        items = [dict(r) for r in conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,))]
        pays = [dict(r, label=PAYMENT_LABELS.get(r["status"], r["status"]))
                for r in conn.execute("SELECT * FROM payments WHERE order_id=? ORDER BY id", (order_id,))]
        ops = [dict(r) for r in conn.execute(
            "SELECT * FROM finance_operations WHERE order_id=? ORDER BY id", (order_id,))]
    finally:
        conn.close()
    cost = sum(i["cost_kop"] * i["qty"] for i in items)
    return {"order": dict(o, pay_label=PAY_LABELS.get(o["pay_status"], o["pay_status"]), total_kop=o["total"] * 100),
            "items": items, "payments": pays, "operations": ops, "cost_kop": cost}
