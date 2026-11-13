"""Настройки из .env. Секреты живут только здесь (на сервере), в браузер они не попадают."""
import os

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> set:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MANAGER_IDS = _ids(os.getenv("MANAGER_IDS", ""))

# "simulator" — учебный имитатор; "yookassa" — тестовый счёт ЮKassa через Telegram
PAYMENT_MODE = os.getenv("PAYMENT_MODE", "simulator").strip().lower()
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "").strip()  # из @BotFather → Payments
YK_SHOP_ID = os.getenv("YK_SHOP_ID", "").strip()           # для возвратов через API
YK_SECRET_KEY = os.getenv("YK_SECRET_KEY", "").strip()     # для возвратов через API

WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")  # публичный HTTPS-адрес
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
INITDATA_MAX_AGE = int(os.getenv("INITDATA_MAX_AGE", "3600"))  # срок жизни initData, сек
RESERVATION_SECONDS = int(os.getenv('RESERVATION_SECONDS', '120'))
WORKER_INTERVAL = float(os.getenv('WORKER_INTERVAL', '5'))
TEST_MODE = os.getenv('SHOP_TEST_MODE') == '1'
if RESERVATION_SECONDS < 1 or WORKER_INTERVAL <= 0:
    raise ValueError('Срок резерва и интервал должны быть положительными')
if PAYMENT_MODE not in ('simulator','yookassa'):
    raise ValueError('PAYMENT_MODE: simulator или yookassa')
if PAYMENT_MODE=='yookassa' and PROVIDER_TOKEN and ':TEST:' not in PROVIDER_TOKEN:
    raise ValueError('Учебный проект принимает только тестовый PROVIDER_TOKEN (:TEST:)')
