"""Human support relay. SQLite contains routing metadata, never message bodies.
All recipients come from server-side tickets; callbacks are authorized on every use.
"""
import asyncio
import sqlite3
import time
from contextlib import closing
from html import escape
from ui import button as ui_button

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

TOPICS = {'pay': 'Оплата и Плюс', 'data': 'Данные', 'bug': 'Ошибка бота', 'other': 'Другой вопрос'}


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(t,d) for t, d in row] for row in rows])


def home():
    return keyboard(*[[(v, 'sup:new:' + k)] for k, v in TOPICS.items()],
                    [('📨 Мои обращения', 'sup:list:0')], [('← Главное меню', 'ui:menu')])


class Support:
    def __init__(self, db, owner, on_home=None):
        self.db, self.owner = db, owner
        self.on_home = on_home
        self._serial = asyncio.Lock()

    def init(self):
        with closing(sqlite3.connect(self.db(), timeout=10)) as c, c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS support_tickets(
              id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL, topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
              created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS support_removed_links(
              bot_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
              ticket_id INTEGER NOT NULL,expires REAL NOT NULL,
              PRIMARY KEY(bot_id,chat_id,message_id));
            CREATE TABLE IF NOT EXISTS support_inboxes(
              bot_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
              created REAL NOT NULL,PRIMARY KEY(bot_id,chat_id,message_id));
            CREATE INDEX IF NOT EXISTS support_ticket_user ON support_tickets(bot_id,user_id,id);
            CREATE TABLE IF NOT EXISTS support_sessions(
              bot_id INTEGER NOT NULL,user_id INTEGER NOT NULL,kind TEXT NOT NULL,
              target TEXT NOT NULL,expires REAL NOT NULL,PRIMARY KEY(bot_id,user_id));
            CREATE TABLE IF NOT EXISTS support_links(
              bot_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
              ticket_id INTEGER NOT NULL,PRIMARY KEY(bot_id,chat_id,message_id));
            CREATE TABLE IF NOT EXISTS support_deliveries(
              bot_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
              ticket_id INTEGER NOT NULL,created REAL NOT NULL,
              PRIMARY KEY(bot_id,chat_id,message_id));
            ''')
            c.execute("INSERT OR REPLACE INTO support_removed_links SELECT l.bot_id,l.chat_id,l.message_id,l.ticket_id,? FROM support_links l JOIN support_tickets t ON t.id=l.ticket_id WHERE t.status='closed'", (time.time()+30*86400,))
            c.execute("DELETE FROM support_sessions WHERE kind IN ('ticket','reply') AND target IN (SELECT CAST(id AS TEXT) FROM support_tickets WHERE status='closed')")
            c.execute("DELETE FROM support_links WHERE ticket_id IN (SELECT id FROM support_tickets WHERE status='closed')")
            c.execute("DELETE FROM support_deliveries WHERE ticket_id IN (SELECT id FROM support_tickets WHERE status='closed')")
            c.execute("DELETE FROM support_tickets WHERE status='closed'")

    def run(self, action, **v):
        now = time.time()
        with closing(sqlite3.connect(self.db(), timeout=10)) as c, c:
            c.row_factory = sqlite3.Row
            if action == 'state':
                r = c.execute('SELECT * FROM support_sessions WHERE bot_id=? AND user_id=?',
                              (v['bid'], v['uid'])).fetchone()
                return dict(r) if r else None
            if action == 'set':
                c.execute('INSERT INTO support_sessions VALUES(?,?,?,?,?) ON CONFLICT(bot_id,user_id) DO UPDATE SET kind=excluded.kind,target=excluded.target,expires=excluded.expires',
                          (v['bid'],v['uid'],v['kind'],str(v['target']),now+1800))
            elif action == 'clear':
                c.execute('DELETE FROM support_sessions WHERE bot_id=? AND user_id=?',(v['bid'],v['uid']))
            elif action == 'create':
                return c.execute('INSERT INTO support_tickets(bot_id,user_id,topic,created,updated) VALUES(?,?,?,?,?)',
                                 (v['bid'],v['uid'],v['topic'],now,now)).lastrowid
            elif action == 'ticket':
                r = c.execute('SELECT * FROM support_tickets WHERE id=? AND bot_id=?',(v['tid'],v['bid'])).fetchone()
                return dict(r) if r else None
            elif action == 'link':
                c.execute('INSERT OR REPLACE INTO support_links VALUES(?,?,?,?)',
                          (v['bid'],v['uid'],v['mid'],v['tid']))
            elif action == 'lookup':
                r=c.execute('SELECT ticket_id FROM support_links WHERE bot_id=? AND chat_id=? AND message_id=?',
                            (v['bid'],v['uid'],v['mid'])).fetchone()
                if r:return r[0]
                r=c.execute('SELECT ticket_id FROM support_removed_links WHERE bot_id=? AND chat_id=? AND message_id=? AND expires>?',
                            (v['bid'],v['uid'],v['mid'],now)).fetchone()
                return r[0] if r else None
            elif action == 'inbox':
                c.execute('INSERT OR REPLACE INTO support_inboxes VALUES(?,?,?,?)',(v['bid'],v['uid'],v['mid'],now))
            elif action == 'delivered':
                return bool(c.execute('SELECT 1 FROM support_deliveries WHERE bot_id=? AND chat_id=? AND message_id=?',
                                      (v['bid'],v['uid'],v['mid'])).fetchone())
            elif action == 'mark':
                c.execute('INSERT OR IGNORE INTO support_deliveries VALUES(?,?,?,?,?)',
                          (v['bid'],v['uid'],v['mid'],v['tid'],now))
                c.execute('UPDATE support_tickets SET updated=? WHERE id=?',(now,v['tid']))
            elif action == 'rate':
                return c.execute('SELECT COUNT(*) FROM support_deliveries WHERE bot_id=? AND chat_id=? AND created>?',
                                 (v['bid'],v['uid'],now-60)).fetchone()[0] >= 10
            elif action == 'close':
                t=c.execute('SELECT * FROM support_tickets WHERE id=? AND bot_id=?',(v['tid'],v['bid'])).fetchone()
                if not t or v.get('uid') not in (self.owner(),t['user_id']):return None
                refs=c.execute('SELECT chat_id,message_id FROM support_links WHERE bot_id=? AND ticket_id=? UNION SELECT chat_id,message_id FROM support_deliveries WHERE bot_id=? AND ticket_id=? UNION SELECT chat_id,message_id FROM support_inboxes WHERE bot_id=? AND chat_id IN (?,?)',
                               (v['bid'],v['tid'],v['bid'],v['tid'],v['bid'],t['user_id'],self.owner())).fetchall()
                # A deleted support reply must not fall through into the AI conversation.
                c.execute('INSERT OR REPLACE INTO support_removed_links SELECT bot_id,chat_id,message_id,ticket_id,? FROM support_links WHERE bot_id=? AND ticket_id=?',(now+30*86400,v['bid'],v['tid']))
                c.execute("DELETE FROM support_sessions WHERE bot_id=? AND kind IN ('ticket','reply') AND target=?",(v['bid'],str(v['tid'])))
                c.execute('DELETE FROM support_links WHERE bot_id=? AND ticket_id=?',(v['bid'],v['tid']))
                c.execute('DELETE FROM support_deliveries WHERE bot_id=? AND ticket_id=?',(v['bid'],v['tid']))
                c.execute('DELETE FROM support_inboxes WHERE bot_id=? AND chat_id IN (?,?)',(v['bid'],t['user_id'],self.owner()))
                c.execute('DELETE FROM support_tickets WHERE id=? AND bot_id=?',(v['tid'],v['bid']))
                return [dict(r) for r in refs]
            elif action == 'list':
                args=[v['bid']]; where="bot_id=? AND status='open'"
                if v['uid'] == self.owner():
                    where += " AND status='open'"
                else:
                    where += ' AND user_id=?';args.append(v['uid'])
                total=c.execute('SELECT COUNT(*) FROM support_tickets WHERE '+where,args).fetchone()[0]
                pages=max(1,(total+7)//8);page=min(max(0,v.get('page',0)),pages-1)
                rows=c.execute('SELECT * FROM support_tickets WHERE '+where+' ORDER BY id DESC LIMIT 8 OFFSET ?',args+[page*8]).fetchall()
                return [dict(r) for r in rows],page,pages
            elif action == 'purge':
                tids=[r[0] for r in c.execute('SELECT id FROM support_tickets WHERE user_id=? UNION SELECT ticket_id FROM support_removed_links WHERE chat_id=?',(v['uid'],v['uid']))]
                for tid in tids:
                    c.execute('DELETE FROM support_removed_links WHERE ticket_id=?',(tid,))
                    c.execute('DELETE FROM support_links WHERE ticket_id=?',(tid,))
                    c.execute('DELETE FROM support_deliveries WHERE ticket_id=?',(tid,))
                    c.execute("DELETE FROM support_sessions WHERE kind IN ('ticket','reply') AND target=?",(str(tid),))
                c.execute('DELETE FROM support_tickets WHERE user_id=?',(v['uid'],))
                c.execute('DELETE FROM support_sessions WHERE user_id=?',(v['uid'],))
                c.execute('DELETE FROM support_removed_links WHERE chat_id=?',(v['uid'],))
                c.execute('DELETE FROM support_inboxes WHERE chat_id=?',(v['uid'],))
            elif action == 'cleanup':
                c.execute('DELETE FROM support_removed_links WHERE expires<?',(now,))
                c.execute('DELETE FROM support_inboxes WHERE created<?',(now-30*86400,))
                # Expired states are retained until explicit navigation or next message.
                # That next message must NOT accidentally reach the model.
                c.execute('DELETE FROM support_sessions WHERE expires<?',(now-30*86400,))
                c.execute("DELETE FROM support_tickets WHERE updated<?",(now-30*86400,))
                c.execute('DELETE FROM support_links WHERE ticket_id NOT IN (SELECT id FROM support_tickets)')
                c.execute('DELETE FROM support_deliveries WHERE created<?',(now-30*86400,))

    async def call(self, action, **v):
        return await asyncio.to_thread(self.run, action, **v)

    async def open(self, message, uid, topic=None):
        async with self._serial:
            return await self._open(message, uid, topic)

    async def _open(self, message, uid, topic=None):
        if self.owner() <= 0:
            await message.answer('Поддержка пока не настроена. Попробуй позже.')
            return
        await self.call('clear',bid=message.bot.id,uid=uid)
        if uid==self.owner() and topic is None:
            await self._listing(message,uid)
            return
        if topic:
            await self.compose(message,uid,topic)
        else:
            kb=home()
            if uid==self.owner():
                kb.inline_keyboard.insert(0,[InlineKeyboardButton(text='📥 Входящие обращения',callback_data='sup:list:0')])
            await message.answer('🛟 Поддержка Гоши\n\nВыбери тему — затем напиши вопрос или пришли скриншот. Ответит владелец бота, не ИИ и не Telegram. Не отправляй пароли и ключи.',reply_markup=kb)

    async def compose(self,message,uid,topic):
        await self.call('set',bid=message.bot.id,uid=uid,kind='compose',target=topic)
        await message.answer('🛟 '+TOPICS[topic]+'\n\nНапиши вопрос следующим сообщением. Можно приложить скриншот с подписью. Сообщение получит владелец; запросы Гоши не тратятся.\n\nНе отправляй пароли, ключи и данные карты. Кнопка ниже вернёт к Гоше.',
                             reply_markup=keyboard([('← Выйти из поддержки','sup:exit')]))

    async def command(self,message):
        async with self._serial:
            return await self._command(message)

    async def _command(self,message):
        cmd=(message.text or '').split(maxsplit=1)
        topic='pay' if cmd[0].split('@')[0]=='/paysupport' else 'other'
        if len(cmd)==1:
            await self._open(message,message.from_user.id,topic if topic=='pay' else None)
        else:
            await self.call('set',bid=message.bot.id,uid=message.from_user.id,kind='compose',target=topic)
            await self._consume(message,override=cmd[1])

    async def listing(self,message,uid,page=0):
        async with self._serial:
            return await self._listing(message,uid,page)

    async def _listing(self,message,uid,page=0):
        rows,page,pages=await self.call('list',bid=message.bot.id,uid=uid,page=page)
        buttons=[]
        for r in rows:
            title=f"#{r['id']:04d} · {r['user_id']} · {TOPICS.get(r['topic'],'Вопрос')}"+(' ✓' if r['status']!='open' else '')
            buttons.append([(title,'sup:view:'+str(r['id']))])
        nav=[]
        if page:nav.append(('←','sup:list:'+str(page-1)))
        if page+1<pages:nav.append(('→','sup:list:'+str(page+1)))
        if nav:buttons.append(nav)
        buttons += [[('＋ Новый вопрос','sup:home')],[('← Главное меню','ui:menu')]]
        sent=await message.answer(('📥 Входящие' if uid==self.owner() else '📨 Мои обращения')+
                             f' · {page+1}/{pages}\n\n'+('Выбери обращение. Сообщения находятся выше в чате.' if rows else 'Пока пусто.'),reply_markup=keyboard(*buttons))
        await self.call('inbox',bid=message.bot.id,uid=uid,mid=sent.message_id)

    def actions(self,tid,owner=False):
        if owner:
            return keyboard([(f'Ответить · №{tid:04d}',f'sup:reply:{tid}'),
                             ('Закрыть тикет',f'sup:close:{tid}')],
                            [('Обращения','sup:list:0')])
        return keyboard([('＋ Дополнить',f'sup:reply:{tid}'),('Закрыть тикет',f'sup:close:{tid}')],
                        [('← К Гоше','sup:exit')])

    @staticmethod
    def chunks(text,limit=2800):
        # Telegram limits are in UTF-16 code units, not Python code points.
        buf=[];units=0
        for char in text:
            width=2 if ord(char)>65535 else 1
            if units+width>limit:
                yield ''.join(buf);buf=[];units=0
            buf.append(char);units+=width
        if buf:yield ''.join(buf)

    async def deliver(self,m,t,target,is_owner,override=None,is_new=False):
        tid=t['id'];kb=self.actions(tid,not is_owner)
        if is_owner:
            header=f"<b>✉ ПОДДЕРЖКА · №{tid:04d}</b>\nОтвет по твоему обращению"
        else:
            name=(getattr(m.from_user,'full_name','') or str(m.from_user.id))[:100]
            state='Новое' if is_new else 'Дополнение'
            header=(f"<b>✉ ОБРАЩЕНИЕ №{tid:04d} · {state}</b>\n"
                    f"<b>Тема:</b> {escape(TOPICS.get(t['topic'],'Вопрос'))}\n"
                    f"<b>От:</b> {escape(name)} · <code>{m.from_user.id}</code>")
        async def remember(sent):
            await self.call('link',bid=m.bot.id,uid=target,mid=sent.message_id,tid=tid)
            return sent
        async def card(text):
            return await remember(await m.bot.send_message(target,header+'\n\n<blockquote>'+escape(text)+'</blockquote>',
                                                        parse_mode='HTML',reply_markup=kb))
        text=override if override is not None else getattr(m,'text',None)
        if text is not None:
            for part in self.chunks(text):await card(part)
            return
        # Media and its identifying caption/buttons travel as ONE Telegram message.
        caption=getattr(m,'caption',None) or ''
        can_caption=any(getattr(m,k,None) for k in ('photo','document','video','animation','audio','voice'))
        if can_caption:
            header_units=len(__import__('re').sub('<[^>]+>','',header).encode('utf-16-le'))//2
            parts=list(self.chunks(caption,max(100,900-header_units))) if caption else []
            cap=header+('\n\n'+escape(parts[0]) if parts else '\n\n📎 Вложение')
            await remember(await m.bot.copy_message(chat_id=target,from_chat_id=m.chat.id,message_id=m.message_id,
                                                   caption=cap,parse_mode='HTML',reply_markup=kb))
            for part in parts[1:]:await card('Продолжение подписи:\n'+part)
        else:
            # Stickers/video notes have no caption field: visually thread the attachment.
            h=await remember(await m.bot.send_message(target,header+'\n\n📎 Вложение ниже',parse_mode='HTML',reply_markup=kb))
            await remember(await m.bot.copy_message(chat_id=target,from_chat_id=m.chat.id,message_id=m.message_id,
                                                   reply_parameters=ReplyParameters(message_id=h.message_id)))

    async def callback(self,q):
        async with self._serial:
            await self._callback(q)

    async def _callback(self,q):
        m,uid,bid=q.message,q.from_user.id,q.message.bot.id
        if m.chat.id!=uid or m.chat.type!=ChatType.PRIVATE:return
        p=(q.data or '').split(':');act=p[1]
        if act=='exit':
            await self.call('clear',bid=bid,uid=uid)
            if self.on_home is not None:
                await self.on_home(m,uid)
            else:
                await m.answer('Режим ввода для поддержки выключен. Нажми «Главное меню», чтобы продолжить.',reply_markup=keyboard([('← Главное меню','ui:menu')]))
        elif act=='home':await self._open(m,uid)
        elif act=='new' and len(p)==3 and p[2] in TOPICS:await self.compose(m,uid,p[2])
        elif act=='list' and len(p)==3 and p[2].isdigit():await self._listing(m,uid,int(p[2]))
        elif act in ('view','reply','close') and len(p)==3 and p[2].isdigit():
            t=await self.call('ticket',bid=bid,tid=int(p[2]))
            if not t or uid not in (self.owner(),t['user_id']):
                if hasattr(m,'edit_reply_markup'):
                    try:await m.edit_reply_markup(reply_markup=None)
                    except TelegramAPIError:pass
                await m.answer('Обращение недоступно.');return
            if act=='view':
                screen=await m.answer(f"✉ ОБРАЩЕНИЕ №{t['id']:04d} · {TOPICS.get(t['topic'],'Вопрос')}\nПользователь: {t['user_id']}\n"+
                               ('Открыто' if t['status']=='open' else 'Закрыто'),
                               reply_markup=self.actions(t['id'],uid==self.owner()) if t['status']=='open' else home())
                await self.call('link',bid=bid,uid=uid,mid=screen.message_id,tid=t['id'])
            elif t['status']!='open':await m.answer('Обращение уже закрыто.',reply_markup=home())
            elif act=='reply':
                await self.call('set',bid=bid,uid=uid,kind='reply' if uid==self.owner() else 'ticket',target=t['id'])
                prompt=await m.answer(f"✍️ {'ОТВЕТ' if uid==self.owner() else 'ДОПОЛНЕНИЕ'} · #{t['id']:04d}\nКому: {t['user_id'] if uid==self.owner() else 'поддержка'}\nТема: {TOPICS.get(t['topic'],'Вопрос')}\n\nСледующее сообщение будет отправлено в это обращение. Можно приложить фото.",reply_markup=keyboard([('Отмена','sup:exit')]))
                await self.call('link',bid=bid,uid=uid,mid=prompt.message_id,tid=t['id'])
            elif act=='close':
                refs=await self.call('close',bid=bid,tid=t['id'],uid=uid)
                if refs is None:
                    await m.answer('Тикет уже удалён.');return
                mid=getattr(m,'message_id',None)
                if isinstance(mid,int):refs.append({'chat_id':uid,'message_id':mid})
                for chat_id,message_id in {(r['chat_id'],r['message_id']) for r in refs}:
                    try:
                        await m.bot.delete_message(chat_id=chat_id,message_id=message_id)
                    except TelegramAPIError:
                        # Telegram may refuse to remove old messages. At least remove their controls.
                        try:await m.bot.edit_message_reply_markup(chat_id=chat_id,message_id=message_id,reply_markup=None)
                        except TelegramAPIError:pass
                await m.answer('✓ Тикет закрыт и удалён.',reply_markup=keyboard([('Обращения','sup:list:0')],[('← Главное меню','ui:menu')]))
                recipient=t['user_id'] if uid==self.owner() else self.owner()
                if recipient!=uid:
                    try:await m.bot.send_message(recipient,f"✓ Тикет №{t['id']:04d} закрыт и удалён.",reply_markup=keyboard([('Обращения','sup:list:0')]))
                    except TelegramAPIError:pass

    async def consume(self,m,override=None):
        async with self._serial:
            return await self._consume(m,override)

    async def _consume(self,m,override=None):
        uid,bid=m.from_user.id,m.bot.id
        state=await self.call('state',bid=bid,uid=uid)
        # A native Telegram reply has priority over a previously selected ticket.
        reply=getattr(m,'reply_to_message',None)
        linked=await self.call('lookup',bid=bid,uid=uid,mid=reply.message_id) if reply else None
        if linked:state={'kind':'reply' if uid==self.owner() else 'ticket','target':str(linked)}
        if getattr(m,'text',None) and m.text.startswith('/') and override is None:
            await self.call('clear',bid=bid,uid=uid);return False
        if reply and not linked and getattr(getattr(reply,'from_user',None),'id',None)==bid:
            body=getattr(reply,'text',None) or getattr(reply,'caption',None) or ''
            if body.startswith(('✉ ОБРАЩЕНИЕ №','✉ ПОДДЕРЖКА · №','✉ TICKET #','✉ SUPPORT · #','✍️ ОТВЕТ · #','✍️ ДОПОЛНЕНИЕ · #','✓ Ответ отправлен · №','✓ Отправлено · #')):
                await m.answer('Тикет недоступен.')
                return True
        if not state:return False
        if not linked and state.get('expires',time.time()+1) <= time.time():
            await self.call('clear',bid=bid,uid=uid)
            await m.answer('Сеанс поддержки истёк. Это сообщение не отправлено ни владельцу, ни ИИ. Открой обращение, чтобы продолжить.',reply_markup=home())
            return True
        if self.owner()<=0:
            await m.answer('Поддержка пока не настроена.');return True
        if not override and not any(getattr(m,k,None) for k in ('text','photo','document','video','voice','audio','sticker','animation')):
            await m.answer('Пришли текст, фото или документ.',reply_markup=keyboard([('Отмена','sup:exit')]))
            return True
        if await self.call('delivered',bid=bid,uid=uid,mid=m.message_id):return True
        if await self.call('rate',bid=bid,uid=uid):
            await m.answer('Слишком много сообщений в поддержку. Подожди минуту. Это сообщение не отправлено.');return True
        if state['kind']=='compose':
            tid=await self.call('create',bid=bid,uid=uid,topic=state['target'])
            await self.call('set',bid=bid,uid=uid,kind='ticket',target=tid)
        else:tid=int(state['target'])
        t=await self.call('ticket',bid=bid,tid=tid)
        is_owner=uid==self.owner() and state['kind']=='reply'
        if not t or (not is_owner and t['user_id']!=uid):
            await self.call('clear',bid=bid,uid=uid);await m.answer('Обращение недоступно.');return True
        if t['status']!='open':
            await self.call('clear',bid=bid,uid=uid);await m.answer('Обращение уже закрыто. Создай новый вопрос.',reply_markup=home());return True
        target=t['user_id'] if is_owner else self.owner()
        try:
            await self.deliver(m,t,target,is_owner,override,is_new=state['kind']=='compose')
        except TelegramAPIError:
            await m.answer('Не удалось доставить сообщение целиком. Попробуй ещё раз. Владелец должен открыть /start в этом боте, а получатель — не блокировать его.',reply_markup=self.actions(tid,is_owner))
            return True
        await self.call('mark',bid=bid,uid=uid,mid=m.message_id,tid=tid)
        if is_owner:
            await self.call('clear',bid=bid,uid=uid)
            ack=await m.answer(f"✓ Ответ отправлен · №{tid:04d}\nОтвет доставлен пользователю {t['user_id']}.",reply_markup=self.actions(tid,True))
            await self.call('link',bid=bid,uid=uid,mid=ack.message_id,tid=tid)
        else:
            await self.call('set',bid=bid,uid=uid,kind='ticket',target=tid)
            ack=await m.answer(f'✓ Отправлено · #{tid:04d}\nОтвет придёт сюда. Можешь дописать детали или нажать «К Гоше».',reply_markup=self.actions(tid))
            await self.call('link',bid=bid,uid=uid,mid=ack.message_id,tid=tid)
        return True


class SupportMiddleware(BaseMiddleware):
    def __init__(self,service):self.service=service
    async def __call__(self,handler,event,data):
        if isinstance(event,Message):
            # Support must never consume payments, service messages or non-private chats.
            if event.chat.type!=ChatType.PRIVATE or event.successful_payment or event.refunded_payment:
                return await handler(event,data)
            if await self.service.consume(event):return
        elif isinstance(event,CallbackQuery):
            if not event.message or event.message.chat.type!=ChatType.PRIVATE:return await handler(event,data)
            if (event.data or '').startswith('sup:'):
                await self.service.callback(event);return
            # Any navigation back into normal bot workflow exits support compose mode.
            if (event.data or '').startswith(('ui:','mode:','pro:','admin:','shop:','credit:','maintenance:','task:')):
                await self.service.call('clear',bid=event.message.bot.id,uid=event.from_user.id)
        return await handler(event,data)
