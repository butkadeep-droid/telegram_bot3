"""Русские подписи и стандартный фон кнопок Telegram без цветных заливок."""
from aiogram.types import InlineKeyboardButton

LABELS = {
 'ui:menu':'← На главную', 'ui:help':'Как общаться', 'ui:help_details':'Подробнее',
 'ui:privacy':'О данных', 'ui:privacy_details':'Подробнее о данных', 'ui:terms':'Условия подписки',
 'ui:support':'Поддержка', 'sup:home':'＋ Новый вопрос', 'sup:list:0':'Обращения',
 'sup:exit':'← К Гоше', 'ui:image':'Создать картинку',
 'ui:agree':'✓ Мне 16+ · продолжить', 'admin:users:0':'Управление ботом',
 'sup:new:pay':'Оплата и подписка', 'sup:new:data':'Мои данные',
 'sup:new:bug':'Ошибка бота', 'sup:new:other':'Другой вопрос',
 'ui:paymentstatus':'Проверить оплату',
}


def button(text,data,style=None):
    # The legacy style argument is accepted but deliberately never sent.
    # Telegram renders buttons using the user's normal theme, not blue/green/red.
    return InlineKeyboardButton(text=LABELS.get(data,text),callback_data=data)
