"""Уведомления в Telegram. Только отправка — финансовые записи здесь не создаются."""
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import config


def webapp_kb():
    if not config.WEBAPP_URL.startswith("https://"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Финансовый кабинет", web_app=WebAppInfo(url=config.WEBAPP_URL))]])


async def managers(bot, text, exclude=()):
    if bot is None:
        return
    for mid in config.MANAGER_IDS:
        if mid in exclude:
            continue
        try:
            await bot.send_message(mid, text, reply_markup=webapp_kb())
        except Exception:
            logging.warning("Не удалось уведомить менеджера %s", mid)


async def customer(bot, user_id, text):
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logging.warning("Не удалось уведомить покупателя %s", user_id)
