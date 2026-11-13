"""Минимальный клиент ЮKassa API (только возвраты). Ключи берутся из config, т.е. из .env."""
import aiohttp

import config

API = "https://api.yookassa.ru/v3"


class YKError(Exception):
    """definitive=True — ЮKassa явно отказала (4xx); False — сеть/5xx, итог возврата неизвестен."""

    def __init__(self, message, definitive):
        super().__init__(message)
        self.definitive = definitive


def _value(kop: int) -> str:
    return f"{kop // 100}.{kop % 100:02d}"


async def _request(method, path, json=None, idem_key=None):
    if not (config.YK_SHOP_ID and config.YK_SECRET_KEY):
        raise YKError("Не заданы YK_SHOP_ID / YK_SECRET_KEY в .env", True)
    headers = {"Idempotence-Key": idem_key} if idem_key else {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.request(method, f"{API}{path}", json=json, headers=headers,
                                 auth=aiohttp.BasicAuth(config.YK_SHOP_ID, config.YK_SECRET_KEY)) as r:
                data = await r.json(content_type=None)
                if r.status >= 500:
                    raise YKError(f"ЮKassa временно недоступна ({r.status})", False)
                if r.status >= 400:
                    raise YKError(data.get("description") or f"Ошибка ЮKassa {r.status}", True)
                return data
    except (aiohttp.ClientError, TimeoutError) as e:
        raise YKError(f"Нет связи с ЮKassa: {type(e).__name__}", False)


async def create_refund(provider_payment_id, amount_kop, currency, idem_key):
    """Повтор с тем же idem_key безопасен: ЮKassa вернёт тот же возврат, а не создаст второй."""
    return await _request("POST", "/refunds", idem_key=idem_key, json={
        "payment_id": provider_payment_id,
        "amount": {"value": _value(amount_kop), "currency": currency},
    })


async def get_refund(refund_id):
    return await _request("GET", f"/refunds/{refund_id}")
