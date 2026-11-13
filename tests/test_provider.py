"""Локальные проверки контракта ЮKassa; внешняя сеть не вызывается."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

import db
import finance
import stock
import worker


class ProviderContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_path=db.DB_PATH
        self.tmp=tempfile.TemporaryDirectory(prefix='shawarma-provider-')
        db.DB_PATH=str(Path(self.tmp.name)/'provider-test.sqlite3');db.init_db()
        db.register_user(1,'Fictional');stock.receive(1,1,'test','provider-test-receipt')
        self.order=stock.checkout(1,[dict(product_id=1,qty=1)],'Fictional','Самовывоз','provider-test-order')['id']
        self.p=stock.create_payment(self.order,1,'yookassa')
        stock.payment_success(self.p['id'],self.p['amount_kop'],'RUB','yk-fictional-payment')
        stock.request_refund(self.order)
        self.data=dict(id='yk-fictional-refund',payment_id='yk-fictional-payment',amount=dict(value='250.00',currency='RUB'),status='succeeded')

    def tearDown(self):
        db.DB_PATH=self.old_path;self.tmp.cleanup()

    def row(self,sql,args=()):
        c=db.get_conn()
        try:return dict(c.execute(sql,args).fetchone())
        finally:c.close()

    async def test_verified_response_completes_once(self):
        with patch.object(worker.yookassa,'create_refund',AsyncMock(return_value=self.data)) as create:
            await worker.process_refunds();await worker.process_refunds()
            self.assertEqual(create.await_count,1)
        self.assertEqual(self.row('SELECT * FROM stock WHERE product_id=1')['on_hand'],1)
        self.assertEqual(self.row('SELECT * FROM refund_jobs')['state'],'refund_succeeded')

    async def test_wrong_payment_currency_amount_cannot_restore_stock(self):
        for change in (dict(payment_id='other'),dict(amount=dict(value='1.00',currency='RUB')),dict(amount=dict(value='250.00',currency='USD'))):
            data=dict(self.data,**change)
            with patch.object(stock,'now',return_value=2000000000),patch.object(worker.yookassa,'create_refund',AsyncMock(return_value=data)):
                c=db.get_conn();c.execute('UPDATE refund_jobs SET next_attempt=0');c.close()
                await worker.process_refunds()
            self.assertEqual(self.row('SELECT * FROM refund_jobs')['state'],'refund_pending')
            self.assertEqual(self.row('SELECT * FROM stock WHERE product_id=1')['on_hand'],0)
            self.assertEqual(self.row("SELECT count(*) AS n FROM finance_operations WHERE type='refund'")['n'],0)

    async def test_expired_idempotence_window_does_not_post_again(self):
        with finance.tx() as c:c.execute('UPDATE refund_jobs SET first_attempt_at=?',(2000000000-24*3600,))
        with patch.object(stock,'now',return_value=2000000000),patch.object(worker.yookassa,'create_refund',AsyncMock(return_value=self.data)) as create:
            await worker.process_refunds();create.assert_not_awaited()
        self.assertIn('срок безопасного повтора',self.row('SELECT * FROM refund_jobs')['last_error'])

    async def test_known_id_is_verified_with_get_after_window(self):
        with finance.tx() as c:
            c.execute('UPDATE refund_jobs SET first_attempt_at=?',(2000000000-24*3600,))
            c.execute('UPDATE payments SET refund_id=?',(self.data['id'],))
        with patch.object(stock,'now',return_value=2000000000),patch.object(worker.yookassa,'get_refund',AsyncMock(return_value=self.data)) as get,patch.object(worker.yookassa,'create_refund',AsyncMock()) as create:
            await worker.process_refunds();get.assert_awaited_once_with(self.data['id']);create.assert_not_awaited()
        self.assertEqual(self.row('SELECT * FROM refund_jobs')['state'],'refund_succeeded')
