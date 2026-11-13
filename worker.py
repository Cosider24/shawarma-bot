"""Восстанавливаемый работник. Вызовы провайдера вне транзакции."""
import asyncio
import logging

import config
import db
import finance
import stock
import yookassa
import notify


def simulator_lookup(operation):
    c=db.get_conn()
    try:
        r=c.execute('SELECT * FROM simulator_refunds WHERE operation_key=?',(operation,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def simulator_create(job,p):
    # Хранилище провайдера независимо от завершения задания в магазине.
    with finance.tx() as c:
        fault=c.execute('SELECT * FROM simulator_faults WHERE payment_id=?',(p['id'],)).fetchone()
        if fault and fault['errors']>0:
            c.execute('UPDATE simulator_faults SET errors=errors-1 WHERE payment_id=?',(p['id'],))
            outcome='error'
        else:
            c.execute("INSERT OR IGNORE INTO simulator_refunds VALUES(?,?,?,'succeeded')",(job['operation_key'],f'sim-refund-{p["id"]}',p['amount_kop']))
            outcome='lost' if fault and fault['lose_response'] else 'ok'
            if fault:
                c.execute('UPDATE simulator_faults SET lose_response=0 WHERE payment_id=?',(p['id'],))
    if outcome=='error':
        raise RuntimeError('Имитатор: временная недоступность')
    if outcome=='lost':
        raise RuntimeError('Имитатор: успешный ответ потерян')
    return simulator_lookup(job['operation_key'])


async def process_refunds(bot=None):
    c=db.get_conn()
    try:
        jobs=[dict(r) for r in c.execute("SELECT * FROM refund_jobs WHERE state='refund_pending' AND next_attempt<=? AND lease_until<=?",(stock.now(),stock.now()))]
    finally:
        c.close()
    for job in jobs:
        with finance.tx() as c:
            claimed=c.execute("UPDATE refund_jobs SET lease_until=?,attempts=attempts+1,first_attempt_at=CASE WHEN first_attempt_at=0 THEN ? ELSE first_attempt_at END WHERE payment_id=? AND state='refund_pending' AND lease_until<=?",(stock.now()+30,stock.now(),job['payment_id'],stock.now())).rowcount
            first_attempt=c.execute('SELECT first_attempt_at FROM refund_jobs WHERE payment_id=?',(job['payment_id'],)).fetchone()[0]
        if not claimed:
            continue
        p=finance.get_payment(job['payment_id'])
        try:
            if p['provider']=='simulator':
                data=await asyncio.to_thread(simulator_lookup,job['operation_key'])
                if not data:
                    data=await asyncio.to_thread(simulator_create,job,p)
                refund_id=data['refund_id']; status=data['status']
            else:
                if not p['refund_id'] and stock.now()-first_attempt>=23*3600:
                    raise RuntimeError('ЮKassa: срок безопасного повтора истёк; сверьте возврат в кабинете провайдера и восстановите refund_id. Новая операция не отправлялась.')
                data=await yookassa.get_refund(p['refund_id']) if p['refund_id'] else await yookassa.create_refund(p['provider_payment_id'],p['amount_kop'],p['currency'],job['operation_key'])
                refund_id=data.get('id'); status=data.get('status')
                amount=data.get('amount') or {}
                if data.get('payment_id')!=p['provider_payment_id'] or amount.get('currency')!=p['currency'] or amount.get('value')!=yookassa._value(p['amount_kop']):
                    raise RuntimeError('Ответ возврата не соответствует исходному платежу, сумме или валюте')
                if refund_id:
                    finance.set_refund_id(p['id'],refund_id)
            if status=='succeeded':
                result=await asyncio.to_thread(stock.complete_refund,p['id'],refund_id)
                if not result['duplicate']:
                    await notify.customer(bot,p['buyer_id'],f"Полный возврат по заказу №{p['order_id']} подтверждён: {finance.fmt_money(p['amount_kop'])}")
                    await notify.managers(bot,f"Возврат заказа №{p['order_id']} подтверждён провайдером.",exclude={p['buyer_id']})
            else:
                raise RuntimeError(f'Провайдер: возврат {status}; требуется повторная проверка')
        except Exception as e:
            await asyncio.to_thread(stock.refund_error,p['id'],str(e))
            logging.warning('Возврат %s: %s',p['id'],e)


async def tick(bot=None):
    await asyncio.to_thread(stock.expire)
    await process_refunds(bot)


async def loop(bot=None):
    while True:
        try:
            await tick(bot)
        except Exception:
            logging.exception('Ошибка фоновой обработки; повтор через интервал')
        await asyncio.sleep(config.WORKER_INTERVAL)


async def lifecycle(app):
    # Восстановление сразу при запуске, затем через интервал.
    await tick(app.get('bot'))
    task=asyncio.create_task(loop(app.get('bot')))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
