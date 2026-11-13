"""Только изолированный стенд: часы из файла, без публичных тестовых API."""
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if os.getenv('SHOP_TEST_MODE')!='1':
    raise RuntimeError('Тестовый сервис требует SHOP_TEST_MODE=1')
import config
import db
import stock
import server
from aiohttp import web

if os.getenv('TEST_CLOCK_FILE'):
    clock=Path(os.environ['TEST_CLOCK_FILE'])
    stock.now=lambda:float(clock.read_text())
db.init_db()
web.run_app(server.create_app(),host='127.0.0.1',port=config.WEB_PORT,print=None,access_log=None)
