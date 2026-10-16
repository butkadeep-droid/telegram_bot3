"""One visual language using Telegram's native blue/green/red button styles."""
from aiogram.types import InlineKeyboardButton

LABELS = {
 'ui:menu':'⌂ Home', 'ui:help':'✧ Quick guide', 'ui:help_details':'＋ Full guide',
 'ui:privacy':'◈ Privacy', 'ui:privacy_details':'＋ Data details', 'ui:terms':'≡ Terms',
 'ui:support':'✉ Support', 'sup:home':'＋ New ticket', 'sup:list:0':'≡ Inbox',
 'sup:exit':'← Back to Gosha', 'ui:image':'🎨 Create image',
 'ui:agree':'✓ Мне 16+ · продолжить', 'admin:users:0':'⚙ Control room',
 'sup:new:pay':'✦ Plus & payments', 'sup:new:data':'◈ Data & privacy',
 'sup:new:bug':'⚒ Report a bug', 'sup:new:other':'✉ Ask a question',
}


def button(text,data,style=None):
    label=LABELS.get(data,text)
    if style is None:
        if data=='ui:agree' or data=='pro:buy':style='success'
        elif data.startswith('sup:reply:') or data in ('ui:support','ui:image'):style='primary'
        elif data.startswith('sup:close:'):style='success'
    return InlineKeyboardButton(text=label,callback_data=data,style=style)
