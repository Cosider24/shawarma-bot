import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption
import db
import bot_store as store
import panels
import stock


class BotChat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old=db.DB_PATH;self.temp=tempfile.TemporaryDirectory();db.DB_PATH=str(Path(self.temp.name)/'test.sqlite3')
        db.init_db();store.init();db.register_user(1,'Test');db.register_user(2,'Other');stock.receive(1,20,'test','chat-test-initial')
    def tearDown(self):db.DB_PATH=self.old;self.temp.cleanup()
    def add(self,add=(),remove=()):
        r=store.new_recipe(1,1)
        for a in add:store.toggle(1,r['token'],'add',a)
        for a in remove:store.toggle(1,r['token'],'remove',a)
        store.add_recipe(1,r['token']);return r

    async def test_twenty_navigation_actions_keep_one_message(self):
        fake=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=99)),edit_message_caption=AsyncMock(),edit_message_media=AsyncMock())
        ids=[]
        for i in range(20):ids.append(await panels.draw(fake,1,1,f'Экран {i}',None,1+i%5))
        self.assertEqual(set(ids),{99});self.assertEqual(fake.send_photo.await_count,1)
        self.assertEqual(fake.edit_message_caption.await_count+fake.edit_message_media.await_count,19)

    async def test_old_text_message_is_converted_without_new_message(self):
        fake=SimpleNamespace(send_photo=AsyncMock(),edit_message_caption=AsyncMock(),edit_message_media=AsyncMock())
        self.assertEqual(await panels.draw(fake,1,1,'Состав',None,2,SimpleNamespace(message_id=12)),12)
        fake.send_photo.assert_not_awaited();fake.edit_message_media.assert_awaited_once()

    async def test_unchanged_and_real_errors_never_send_fallback_spam(self):
        store.save_panel(1,1,7,1)
        method=EditMessageCaption(chat_id=1,message_id=7,caption='x')
        fake=SimpleNamespace(send_photo=AsyncMock(),edit_message_caption=AsyncMock(side_effect=TelegramBadRequest(method=method,message='message is not modified')),edit_message_media=AsyncMock())
        await panels.draw(fake,1,1,'x',None);fake.send_photo.assert_not_awaited()
        fake.edit_message_caption.side_effect=TelegramBadRequest(method=method,message='caption is too long')
        with self.assertRaises(TelegramBadRequest):await panels.draw(fake,1,1,'x',None)
        fake.send_photo.assert_not_awaited()

    async def test_deleted_panel_recovers_once(self):
        store.save_panel(1,1,7,1)
        fake=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=8)),edit_message_caption=AsyncMock(side_effect=TelegramBadRequest(method=EditMessageCaption(chat_id=1,message_id=7,caption='x'),message='message to edit not found')))
        self.assertEqual(await panels.draw(fake,1,1,'x',None),8)
        self.assertEqual(store.panel(1)['message_id'],8)

    async def test_variants_recipe_price_persistence_and_double_add(self):
        r=self.add(['jalapeno','cheese'],['Майонез']);self.assertFalse(store.add_recipe(1,r['token']))
        self.add();rows=store.cart(1);self.assertEqual(len(rows),2);self.assertEqual([r['price'] for r in rows],[320,250])
        store.init();self.assertEqual(len(store.cart(1)),2)
        self.assertIn('без майонеза',rows[0]['name'])
        with self.assertRaises(stock.ShopError):store.change_line(2,rows[0]['id'],1)

    async def test_checkout_reserves_variants_and_clears_cart_atomically(self):
        self.add(['jalapeno'],['Лук']);self.add();data=store.prepare_checkout(1,'Test');token=data['checkout']['request_key']
        result=store.confirm(1,token);self.assertFalse(result['duplicate']);self.assertTrue(store.confirm(1,token)['duplicate'])
        self.assertEqual(store.cart(1),[])
        c=db.get_conn()
        try:
            self.assertEqual(c.execute('SELECT total FROM orders').fetchone()[0],530)
            self.assertEqual(c.execute('SELECT qty FROM reservation_items').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT reserved FROM stock WHERE product_id=1').fetchone()[0],2)
        finally:c.close()

    async def test_changed_cart_and_shortage_leave_cart_and_orders_consistent(self):
        r=self.add();data=store.prepare_checkout(1,'Test');line=store.cart(1)[0]
        store.change_line(1,line['id'],1)
        with self.assertRaises(stock.ShopError):store.confirm(1,data['checkout']['request_key'])
        c=db.get_conn()
        try:self.assertEqual(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
        finally:c.close()
        data=store.prepare_checkout(1,'Test')
        # Actual competitor reserves all 20 units through the normal server method.
        stock.checkout(2,[dict(product_id=1,qty=20)],'Other','Самовывоз','competitor-unique-key')
        with self.assertRaises(stock.ShopError):store.confirm(1,data['checkout']['request_key'])
        self.assertEqual(store.cart(1)[0]['qty'],2)

    async def test_legacy_cart_migrates_once(self):
        db.change_qty(1,1,3);store.init();store.init()
        self.assertEqual(store.cart(1)[0]['qty'],3)

    async def test_every_recipe_button_is_short_and_all_selected_fits_caption(self):
        import bot_recipe
        r=store.new_recipe(1,1)
        for name in stock.ADDITIONS:r=store.toggle(1,r['token'],'add',name)
        for name in stock.INGREDIENTS[1]:r=store.toggle(1,r['token'],'remove',name)
        fake=SimpleNamespace(send_photo=AsyncMock(),edit_message_media=AsyncMock(),edit_message_caption=AsyncMock())
        fake_cb=SimpleNamespace(bot=fake,from_user=SimpleNamespace(id=1),message=SimpleNamespace(chat=SimpleNamespace(id=1),message_id=8),answer=AsyncMock())
        await bot_recipe.product(fake_cb,r)
        media=fake.edit_message_media.call_args.kwargs['media'];self.assertLessEqual(len(media.caption),1024)
        for row in fake.edit_message_media.call_args.kwargs['reply_markup'].inline_keyboard:
            for button in row:
                if button.callback_data:self.assertLessEqual(len(button.callback_data.encode()),64)
