"""Одна редактируемая панель на пользователя; фото тоже меняется на месте."""
from pathlib import Path
import html
import re
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile,InputMediaPhoto
import bot_store as store


async def draw(bot,uid,chat_id,text,markup,image_id=1,source=None):
    if len(text)>1024:
        plain=html.unescape(re.sub(r'<[^>]*>','',text))
        text=html.escape(plain[:800])+'\n… Полный состав — в Mini App.'
    image_id=image_id if image_id in range(1,7) else 1
    image=Path(__file__).parent/'webapp/assets'/f'product-{image_id}.png'
    current=store.panel(uid)
    if current and current['chat_id']!=chat_id:current=None
    if current is None and source is not None:
        current=dict(chat_id=chat_id,message_id=source.message_id,image_id=-1)
    if current:
        try:
            if current['image_id']==image_id:
                await bot.edit_message_caption(chat_id=chat_id,message_id=current['message_id'],caption=text,reply_markup=markup,parse_mode='HTML')
            else:
                await bot.edit_message_media(chat_id=chat_id,message_id=current['message_id'],media=InputMediaPhoto(media=FSInputFile(image),caption=text,parse_mode='HTML'),reply_markup=markup)
            store.save_panel(uid,chat_id,current['message_id'],image_id)
            return current['message_id']
        except TelegramBadRequest as error:
            description=str(error).lower()
            if 'message is not modified' in description:
                store.save_panel(uid,chat_id,current['message_id'],image_id)
                return current['message_id']
            if 'message to edit not found' not in description and "message can't be edited" not in description:
                raise
    sent=await bot.send_photo(chat_id=chat_id,photo=FSInputFile(image),caption=text,reply_markup=markup,parse_mode='HTML')
    store.save_panel(uid,chat_id,sent.message_id,image_id)
    return sent.message_id


async def message(message,text,markup,image_id=1):
    return await draw(message.bot,message.from_user.id,message.chat.id,text,markup,image_id)
