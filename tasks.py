"""Separate, persistent task evidence inbox. No AI calls, no message bodies in SQLite."""
import asyncio
import sqlite3
import time
from contextlib import closing
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.exceptions import TelegramAPIError
from ui import button

REWARD=5_000_000
CAMPAIGN='tiktok20-v1'

def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[button(t,d) for t,d in row] for row in rows])

def actions(tid):
    return kb([('📨 Отправить на проверку',f'task:submit:{tid}')], [('← Задания','task:home')], [('← Главное меню','ui:menu')])

class Tasks:
    def __init__(self,db,owner,eco,check,cleanup=None):
        self.db,self.owner,self.eco,self.check=db,owner,eco,check
        self.cleanup=cleanup
    def init(self):
        with closing(sqlite3.connect(self.db(),timeout=15)) as c,c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS task_submissions(
              id INTEGER PRIMARY KEY AUTOINCREMENT,bot_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
              campaign TEXT NOT NULL,username TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',
              created REAL NOT NULL,updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS task_user ON task_submissions(bot_id,user_id,campaign);
            CREATE TABLE IF NOT EXISTS task_evidence(
              submission_id INTEGER NOT NULL,message_id INTEGER NOT NULL,file_uid TEXT NOT NULL,
              owner_mid INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'reserved',
              PRIMARY KEY(submission_id,message_id),UNIQUE(submission_id,file_uid));
            CREATE TABLE IF NOT EXISTS task_sessions(
              bot_id INTEGER NOT NULL,user_id INTEGER NOT NULL,submission_id INTEGER NOT NULL,
              PRIMARY KEY(bot_id,user_id));
            ''')
            c.execute("DELETE FROM task_evidence WHERE state='reserved'")
    def run(self,action,bid,uid,tid=0,**v):
        now=time.time()
        with closing(sqlite3.connect(self.db(),timeout=15)) as c,c:
            c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
            if action=='clear':
                c.execute('DELETE FROM task_sessions WHERE bot_id=? AND user_id=?',(bid,uid));return
            if action=='session':
                row=c.execute('SELECT t.* FROM task_sessions s JOIN task_submissions t ON t.id=s.submission_id WHERE s.bot_id=? AND s.user_id=?',(bid,uid)).fetchone()
                return dict(row) if row else None
            if action=='list':
                if not self.owner() or uid!=self.owner():return []
                return [dict(r) for r in c.execute("SELECT * FROM task_submissions WHERE bot_id=? AND status='pending' ORDER BY id LIMIT 30",(bid,))]
            if action=='latest':
                row=c.execute('SELECT * FROM task_submissions WHERE bot_id=? AND user_id=? AND campaign=? ORDER BY id DESC LIMIT 1',(bid,uid,CAMPAIGN)).fetchone()
                return dict(row) if row else None
            if action=='start':
                # An approved campaign is permanently completed, even after /delete.
                if c.execute("SELECT 1 FROM task_submissions WHERE user_id=? AND campaign=? AND status='approved'",(uid,CAMPAIGN)).fetchone():return {'status':'approved'}
                row=c.execute("SELECT * FROM task_submissions WHERE bot_id=? AND user_id=? AND campaign=? AND status IN ('draft','pending') ORDER BY id DESC LIMIT 1",(bid,uid,CAMPAIGN)).fetchone()
                if row and row['status']=='pending':return dict(row)
                if row:tid=row['id']
                else:tid=c.execute('INSERT INTO task_submissions(bot_id,user_id,campaign,username,created,updated) VALUES(?,?,?,?,?,?)',(bid,uid,CAMPAIGN,v.get('username',''),now,now)).lastrowid
                c.execute('INSERT INTO task_sessions VALUES(?,?,?) ON CONFLICT(bot_id,user_id) DO UPDATE SET submission_id=excluded.submission_id',(bid,uid,tid))
                return dict(c.execute('SELECT * FROM task_submissions WHERE id=?',(tid,)).fetchone())
            row=c.execute('SELECT * FROM task_submissions WHERE id=? AND bot_id=?',(tid,bid)).fetchone()
            if not row or (row['user_id']!=uid and uid!=self.owner()):return None
            if action=='get':return dict(row)
            if action=='proofs':
                if uid!=self.owner():return []
                return [r[0] for r in c.execute("SELECT owner_mid FROM task_evidence WHERE submission_id=? AND state='sent' ORDER BY message_id LIMIT 5 OFFSET ?",(tid,v.get('page',0)*5))]
            if action=='count':return c.execute("SELECT COUNT(*) FROM task_evidence WHERE submission_id=? AND state='sent'",(tid,)).fetchone()[0]
            if action=='reserve':
                if row['user_id']!=uid or row['status']!='draft':return False
                if c.execute('SELECT COUNT(*) FROM task_evidence WHERE submission_id=?',(tid,)).fetchone()[0]>=20:return False
                return c.execute('INSERT OR IGNORE INTO task_evidence(submission_id,message_id,file_uid) VALUES(?,?,?)',(tid,v['mid'],v['file_uid'])).rowcount==1
            if action=='delivered':
                c.execute("UPDATE task_evidence SET state='sent',owner_mid=? WHERE submission_id=? AND message_id=?",(v['owner_mid'],tid,v['mid']));return True
            if action=='failed':
                c.execute("DELETE FROM task_evidence WHERE submission_id=? AND message_id=? AND state='reserved'",(tid,v['mid']));return True
            if action=='submit':
                if row['user_id']!=uid or row['status']!='draft':return False
                n=c.execute("SELECT COUNT(*) FROM task_evidence WHERE submission_id=? AND state='sent'",(tid,)).fetchone()[0]
                if n!=20:return False
                c.execute("UPDATE task_submissions SET status='pending',updated=? WHERE id=?",(now,tid))
                c.execute('DELETE FROM task_sessions WHERE bot_id=? AND user_id=?',(bid,uid));return True
    async def call(self,action,bid,uid,tid=0,**v):
        return await asyncio.to_thread(self.run,action,bid,uid,tid,**v)
    async def home(self,m,uid):
        await self.call('clear',m.bot.id,uid)
        item=await self.call('latest',m.bot.id,uid)
        status={'draft':'Черновик сохранён — можно продолжить.','pending':'Заявка на проверке у владельца.','approved':'Задание выполнено, награда начислена.','rejected':'Заявка отклонена. Можно прислать новые доказательства.'}.get(item['status'] if item else '', '')
        text=('🎯 Задания\n\n📣 Расскажи о Гоше под 20 разными видео в TikTok.\n'
              'В комментарии укажи @GoshaAIrobot: это ИИ-помощник, с которым можно поболтать, если скучно. Наш канал: @Gosha_Ai_Channel. Пиши своими словами и только там, где это уместно и реклама разрешена. Не дублируй спам.\n\n'
              '📸 Сохрани 20 скриншотов: на каждом видны видео и твой опубликованный комментарий. Загрузи их сюда и отправь заявку на проверку.\n'
              '🎁 После одобрения владельцем — 5 000 000 токенов. Награда за это задание выдаётся один раз. Проверка ручная, не мгновенная.\n\n'+status)
        rows=[]
        if not item or item['status'] in ('draft','rejected'):rows.append([('📎 Отправить доказательства','task:start')])
        if uid==self.owner():rows.append([('📥 Заявки на проверку','task:inbox')])
        rows.append([('← Главное меню','ui:menu')])
        await m.answer(text,parse_mode=None,reply_markup=kb(*rows))
    def review_kb(self,tid):
        return kb([('📸 Смотреть скриншоты',f'task:proofs:{tid}:0')],[('✅ Одобрить · +5 млн',f'task:approve:{tid}')],[('✕ Отклонить',f'task:reject:{tid}')],[('📥 Все заявки','task:inbox')])
    async def notify_review(self,bot,item):
        tag='@'+item['username'] if item['username'] else 'без username'
        await bot.send_message(self.owner(),f'📋 ЗАДАНИЕ · заявка №{item["id"]}\nПользователь: {tag}\nID: {item["user_id"]}\n\n20 скриншотов отправлены выше с номером заявки. Проверь разные видео и комментарии.\nНаграда: 5 000 000 токенов.',parse_mode=None,reply_markup=self.review_kb(item['id']))
    async def callback(self,q):
        m,uid,bid=q.message,q.from_user.id,q.message.bot.id
        if m.chat.type!='private' or m.chat.id!=uid:return
        await q.answer()
        if not await self.check(m,uid):return
        parts=(q.data or '').split(':');act=parts[1] if len(parts)>1 else ''
        if act=='home':await self.home(m,uid);return
        if act=='start':
            if self.owner()<=0:await m.answer('Приём заявок пока недоступен.');return
            item=await self.call('start',bid,uid,username=q.from_user.username or '')
            if item['status']!='draft':await self.home(m,uid);return
            n=await self.call('count',bid,uid,item['id'])
            await m.answer(f'📎 Доказательства · №{item["id"]}\n\nОтправь 20 скриншотов — по одному или альбомом. Можно добавить подпись со ссылкой на видео. Получено: {n}/20.\n\nКогда загрузишь всё, нажми «Отправить на проверку». Скриншоты получит владелец, не ИИ. При выходе черновик сохранится.',parse_mode=None,reply_markup=actions(item['id']));return
        if act=='inbox':
            if uid!=self.owner():await m.answer('⛔ Только для владельца.');return
            rows=await self.call('list',bid,uid)
            await m.answer('📥 Заявки на проверку'+('\nПоказаны первые 30.' if len(rows)==30 else '') if rows else '📥 Новых заявок нет.',reply_markup=kb(*[[ (f'№{r["id"]} · {r["user_id"]}',f'task:view:{r["id"]}') ] for r in rows],[('← Главное меню','ui:menu')]));return
        if act=='proofs':
            if uid!=self.owner() or len(parts)!=4 or not all(x.isascii() and x.isdigit() and len(x)<12 for x in parts[2:]):return
            tid,page=int(parts[2]),int(parts[3])
            if page>3:return
            proofs=await self.call('proofs',bid,uid,tid,page=page)
            if not proofs:await m.answer('Скриншоты недоступны.');return
            for mid in proofs:
                try:await m.bot.copy_message(chat_id=uid,from_chat_id=uid,message_id=mid)
                except TelegramAPIError:await m.answer('Один из скриншотов недоступен в Telegram.')
            rows=[]
            if page<3:rows.append([('Следующие 5 →',f'task:proofs:{tid}:{page+1}')])
            rows.append([('← К заявке',f'task:view:{tid}')])
            await m.answer(f'📸 Заявка №{tid} · скриншоты {page*5+1}–{page*5+len(proofs)}',reply_markup=kb(*rows));return
        if len(parts)!=3 or not parts[2].isascii() or not parts[2].isdigit() or len(parts[2])>12:return
        tid=int(parts[2]);item=await self.call('get',bid,uid,tid)
        if not item:await m.answer('Заявка недоступна.');return
        if act=='submit':
            if item['user_id']!=uid:return
            ok=await self.call('submit',bid,uid,tid)
            if not ok:
                n=await self.call('count',bid,uid,tid)
                await m.answer(f'Нужно 20 скриншотов. Получено: {n}/20.' if item['status']=='draft' else 'Заявка уже отправлена или обработана.',reply_markup=actions(tid) if item['status']=='draft' else kb([('← Задания','task:home')]));return
            try:await self.notify_review(m.bot,item)
            except TelegramAPIError:await m.answer('Заявка сохранена в очереди владельца.');return
            await m.answer('✅ Заявка отправлена! После проверки ответ придёт сюда.',reply_markup=kb([('← Главное меню','ui:menu')]));return
        if uid!=self.owner():await m.answer('⛔ Только для владельца.');return
        if act=='view':
            if item['status']=='pending':await self.notify_review(m.bot,item)
            else:await m.answer('Заявка уже обработана.')
        elif act in ('approve','reject'):
            result=await self.eco('task_review',uid,dict(tid=tid,bid=bid,approve=act=='approve'))
            if not result:await m.answer('Заявка уже обработана, пользователь заблокирован или действие недоступно.');return
            try:await m.edit_reply_markup(reply_markup=None)
            except TelegramAPIError:pass
            await m.answer(f'✅ Заявка №{tid}: +5 000 000 токенов.' if act=='approve' else f'Заявка №{tid} отклонена.')
            try:await m.bot.send_message(result,'🎁 Задание одобрено! Начислено 5 000 000 токенов. /balance' if act=='approve' else '📋 Заявка отклонена: доказательства не прошли проверку. Открой «Задания», проверь условия и отправь новые скриншоты.',parse_mode=None)
            except TelegramAPIError:pass
    async def consume(self,m):
        uid,bid=m.from_user.id,m.bot.id
        item=await self.call('session',bid,uid)
        if not item:return False
        if item['status']!='draft':
            await self.call('clear',bid,uid);await m.answer('Заявка уже отправлена. /menu');return True
        if m.photo:file=m.photo[-1]
        elif m.document and (m.document.mime_type or '') in ('image/png','image/jpeg','image/webp'):file=m.document
        else:
            await m.answer('📸 Жду скриншот. Отправь изображение или вернись в главное меню.',reply_markup=actions(item['id']));return True
        tid=item['id']
        ok=await self.call('reserve',bid,uid,tid,mid=m.message_id,file_uid=file.file_unique_id)
        if not ok:
            n=await self.call('count',bid,uid,tid)
            await m.answer(f'Этот скриншот уже добавлен или достигнут лимит. Получено: {n}/20.',reply_markup=actions(tid));return True
        try:
            n=await self.call('count',bid,uid,tid)
            tag='@'+m.from_user.username if m.from_user.username else 'без username'
            header=f'📎 ЗАДАНИЕ №{tid} · скриншот {n+1}/20\n{tag} · ID {uid}'
            if m.caption:header+='\n\n'+m.caption[:600]
            copied=await m.bot.copy_message(chat_id=self.owner(),from_chat_id=uid,message_id=m.message_id,caption=header,parse_mode=None)
            await self.call('delivered',bid,uid,tid,mid=m.message_id,owner_mid=copied.message_id)
        except TelegramAPIError:
            await self.call('failed',bid,uid,tid,mid=m.message_id)
            await m.answer('Не удалось доставить скриншот владельцу. Попробуй ещё раз — он не засчитан.');return True
        n=await self.call('count',bid,uid,tid)
        # Albums are individual Telegram updates: only the final one needs a keyboard.
        if not m.media_group_id or n==20:
            await m.answer(f'📎 Получено {n}/20.'+(' Всё готово — отправь заявку на проверку.' if n==20 else ''),reply_markup=actions(tid))
        return True

class TasksMiddleware(BaseMiddleware):
    def __init__(self,service,main):self.service,self.main=service,main
    async def __call__(self,handler,event,data):
        m=event.message if isinstance(event,CallbackQuery) else event
        u=getattr(event,'from_user',None);main=self.main()
        if not isinstance(m,Message) or not u or u.is_bot or not main or m.bot.id!=main.id or m.chat.type!='private' or m.chat.id!=u.id:return await handler(event,data)
        if isinstance(event,Message):
            if event.successful_payment or event.refunded_payment:return await handler(event,data)
            if (event.text or '').startswith('/'):
                await self.service.call('clear',m.bot.id,u.id)
            elif await self.service.consume(event):return
        else:
            if (event.data or '').startswith('task:'):
                await self.service.callback(event)
                if event.data in ('task:home','task:start','task:inbox') and self.service.cleanup:
                    await self.service.cleanup(m,u.id)
                return
            if (event.data or '').startswith(('ui:','mode:','pro:','sup:','admin:','shop:','credit:','maintenance:')):
                await self.service.call('clear',m.bot.id,u.id)
        return await handler(event,data)
