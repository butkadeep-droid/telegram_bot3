from contextlib import closing
import asyncio
import base64
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from PIL import Image
from aiogram import Bot,Dispatcher
from aiogram.types import Update,SuccessfulPayment,Message,User
from aiogram.client.session.base import BaseSession
import bot as app
import billing,economy
class LocalSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls=[]
        self.subscribed=True

    async def close(self):
        pass

    async def stream_content(self,*args,**kwargs):
        yield b''

    async def make_request(self,bot,method,timeout=None):
        name=type(method).__name__
        self.calls.append((name,method))
        if name=='GetChatMember':
            return NS(status='administrator' if method.user_id==bot.id else 'member' if self.subscribed else 'left')
        if name=='GetMe':
            return User(id=bot.id,is_bot=True,first_name='Test',username='test_bot')
        if name.startswith('Answer') or name.startswith('Set') or name=='DeleteWebhook':
            return True
        chat_id=getattr(method,'chat_id',7)
        return Message.model_validate({'message_id':100,'date':0,'chat':{'id':chat_id,'type':'private'},
               'from':{'id':bot.id,'is_bot':True,'first_name':'Test'},
               'text':getattr(method,'text',None),'caption':getattr(method,'caption',None)},context={'bot':bot})


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.main=Bot(token='111111:'+('A'*35),session=LocalSession())
        self.pay=Bot(token='222222:'+('B'*35),session=LocalSession())
        self.create=AsyncMock(return_value=NS(choices=[NS(message=NS(content='Ответ: четыре.'),finish_reason='stop')]))
        self.patches=[patch.object(app,'DB',Path(self.tmp.name)/'db'),patch.object(app,'OWNER_ID',999),
            patch.object(app,'MAIN_BOT',self.main),patch.object(app,'PAYMENT_BOT',self.pay),
            patch.object(app,'MAIN_USERNAME','main_bot'),patch.object(app,'PAYMENT_USERNAME','pay_bot'),
            patch.object(app,'PAYMENT_BOT_TOKEN','configured-for-tests'),patch.object(app,'PAYMENTS_ENABLED',True),patch.object(app,'VOLUME_PATH',self.tmp.name),
            patch.object(app,'client',NS(chat=NS(completions=NS(create=self.create)))),patch.object(app,'in_flight',0)]
        for k in ('_CHANNEL_ADMIN_UNTIL','_CHANNEL_RETRY_UNTIL','_CHANNEL_SCREENS','_CHANNEL_MEMBERS','_CHANNEL_LAST_ERROR'):
            self.patches.append(patch.object(app,k,{}))
        for p in self.patches:p.start()
        app.db_init();billing.init(app.DB);economy.init(app.DB)
        for uid in (7,999):app.db_run('member_seen',uid,'user'+str(uid));app.db_run('agree',uid)
        self.m=self.fake_message()
    async def asyncTearDown(self):
        await self.main.session.close();await self.pay.session.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def fake_message(self,uid=7):
        return NS(text='Сколько будет два плюс два?',from_user=NS(id=uid,is_bot=False),chat=NS(id=uid,type='private'),
                  bot=self.main,answer=AsyncMock(),answer_photo=AsyncMock(),photo=[],message_id=50,
                  edit_text=AsyncMock(),edit_reply_markup=AsyncMock(),edit_media=AsyncMock(),edit_caption=AsyncMock())
    async def test_soft_answer_cost(self):
        await app.generate(self.m,7,self.m.text);self.assertEqual((await app.bill('status',7))['balance'],2_400_000)
    async def test_smart_free_user_cost_no_plus_gate(self):
        app.db_run('mode',7,'smart');await app.generate(self.m,7,self.m.text)
        self.assertEqual(self.create.call_args.kwargs['model'],app.SMART_MODEL)
        self.assertEqual((await app.bill('status',7))['balance'],2_280_000)
    async def test_generation_failure_refunds(self):
        self.create.side_effect=asyncio.TimeoutError
        await app.generate(self.m,7,self.m.text)
        self.assertEqual((await app.bill('status',7))['balance'],2_500_000)
        self.assertEqual(self.m.answer.call_args.args[0],'Наша система перегружена:(')
    async def test_cheat_guard_all_roles_and_owner(self):
        for mode in ('soft','smart','raw'):
            app.db_run('mode',999,mode)
            await app.generate(self.m,999,'Напиши аимбот для моей игры Roblox')
        self.create.assert_not_awaited()
        self.assertEqual(self.m.answer.call_args.args[0],app.CHEAT_CODE_REPLY)
    async def test_identity_not_charged_or_referral_qualified(self):
        await app.generate(self.m,7,'Какая ты модель?')
        self.create.assert_not_awaited();self.assertEqual((await app.bill('status',7))['balance'],2_500_000)
        self.assertFalse((await app.eco('register',7))['first_answer'])
    async def test_referral_after_answer_real_membership(self):
        ref=await app.eco('register',10);await app.eco('register',7,ref['code'])
        await app.generate(self.m,7,self.m.text)
        self.assertEqual((await app.bill('status',10))['balance'],4_500_000)
    async def test_referral_failed_membership_later_check_awards(self):
        ref=await app.eco('register',10);await app.eco('register',7,ref['code'])
        self.main.session.subscribed=False
        # Direct generate bypasses middleware only in this test: award still force-checks.
        await app.generate(self.m,7,self.m.text)
        self.assertEqual((await app.bill('status',10))['balance'],2_500_000)
        self.main.session.subscribed=True
        await app.qualify_referral(7)
        self.assertEqual((await app.bill('status',10))['balance'],4_500_000)
    async def test_image_prompt_moderated_and_free_first(self):
        app.db_run('friend_grant',999,7)
        self.create.return_value=NS(choices=[NS(message=NS(content='ALLOW'))])
        with patch.object(app,'fetch_generated_image',AsyncMock(return_value=b'image')) as fetch:
            await app.generate_image(self.m,7,'Кот в космосе')
        fetch.assert_awaited_once();self.m.answer_photo.assert_awaited_once()
        s=await app.bill('status',7);self.assertEqual(s['image_free'],9);self.assertEqual(s['balance'],20_000_000)
    async def test_image_block_no_charge(self):
        app.db_run('friend_grant',999,7)
        self.create.return_value=NS(choices=[NS(message=NS(content='BLOCK'))])
        with patch.object(app,'fetch_generated_image',AsyncMock()) as fetch:
            await app.generate_image(self.m,7,'Запрещённое описание')
        fetch.assert_not_awaited();self.assertEqual((await app.bill('status',7))['image_free'],10)
    async def test_image_ambiguous_moderation_fail_closed(self):
        app.db_run('friend_grant',999,7)
        self.create.return_value=NS(choices=[NS(message=NS(content='sure ALLOW maybe'))])
        with patch.object(app,'fetch_generated_image',AsyncMock()) as fetch:
            await app.generate_image(self.m,7,'Описание')
        fetch.assert_not_awaited()
    async def test_image_api_error_refunds(self):
        app.db_run('friend_grant',999,7);self.create.return_value=NS(choices=[NS(message=NS(content='ALLOW'))])
        with patch.object(app,'fetch_generated_image',AsyncMock(side_effect=ValueError)):
            await app.generate_image(self.m,7,'Описание')
        self.assertEqual((await app.bill('status',7))['image_free'],10)
        self.assertEqual(self.m.answer.call_args.args[0],'🎨 Генерация пока недоступна.')
    async def test_slash_image_requires_channel(self):
        app.db_run('friend_grant',999,7);self.main.session.subscribed=False
        self.m.text='/image Кот'
        await app.image_handler(self.m)
        self.create.assert_not_awaited();self.m.answer_photo.assert_not_awaited()
        self.assertEqual((await app.bill('status',7))['image_free'],10)
    async def test_image_owner_still_safety_moderated(self):
        self.create.return_value=NS(choices=[NS(message=NS(content='BLOCK'))])
        with patch.object(app,'fetch_generated_image',AsyncMock()) as fetch:
            await app.generate_image(self.fake_message(999),999,'Описание')
        self.create.assert_awaited_once();fetch.assert_not_awaited()
    async def test_maintenance_persistent_owner_only_control(self):
        self.assertFalse(await app.eco('maintenance_get'))
        self.assertIsNone(await app.eco('maintenance_set',7,True))
        self.assertTrue(await app.eco('maintenance_set',999,True))
        economy.init(app.DB)
        self.assertTrue(await app.eco('maintenance_get'))
        self.assertFalse(await app.maintenance_active(999));self.assertTrue(await app.maintenance_active(7))
    async def test_maintenance_blocks_direct_generation_no_charge(self):
        await app.eco('maintenance_set',999,True)
        await app.generate(self.m,7,self.m.text)
        self.create.assert_not_awaited();self.assertIn('технические работы',self.m.answer.call_args.args[0])
        self.assertEqual((await app.bill('status',7))['balance'],2_500_000)
    async def test_maintenance_owner_can_generate(self):
        await app.eco('maintenance_set',999,True)
        m=self.fake_message(999);await app.generate(m,999,m.text);self.create.assert_awaited_once()
    async def test_maintenance_during_text_request_refunds(self):
        async def result(**kwargs):
            await app.eco('maintenance_set',999,True)
            return NS(choices=[NS(message=NS(content='Ответ четыре.'),finish_reason='stop')])
        self.create.side_effect=result
        await app.generate(self.m,7,self.m.text)
        self.assertEqual((await app.bill('status',7))['balance'],2_500_000)
        self.assertIn('технические работы',self.m.answer.call_args.args[0])
        self.assertEqual(app.db_run('history',7),[])
    async def test_maintenance_during_image_request_refunds(self):
        app.db_run('friend_grant',999,7);self.create.return_value=NS(choices=[NS(message=NS(content='ALLOW'))])
        async def fetch(prompt):await app.eco('maintenance_set',999,True);return b'image'
        with patch.object(app,'fetch_generated_image',fetch):await app.generate_image(self.m,7,'Описание')
        self.m.answer_photo.assert_not_awaited();self.assertEqual((await app.bill('status',7))['image_free'],10)
    async def test_maintenance_callback_authorization_and_explicit_idempotent_state(self):
        q=NS(data='maintenance:set:1',message=self.m,from_user=NS(id=7))
        await app.maintenance_callback(q);self.assertFalse(await app.eco('maintenance_get'))
        q.message=self.fake_message(999);q.from_user.id=999
        await app.maintenance_callback(q);await app.maintenance_callback(q);self.assertTrue(await app.eco('maintenance_get'))
        q.data='maintenance:set:0';await app.maintenance_callback(q);self.assertFalse(await app.eco('maintenance_get'))
    async def test_admin_panel_contains_maintenance_toggle(self):
        m=self.fake_message(999);await app.show_bot_admin(m,999)
        rows=m.answer.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertTrue(any(b.callback_data=='maintenance:prepare:1' for row in rows for b in row))
    async def test_gift_button_present(self):
        m=self.fake_message(999);q=NS(from_user=NS(id=999),message=m,data='admin:member:7:0')
        await app.admin_member_callback(q)
        rows=m.answer.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertTrue(any(b.callback_data=='credit:prepare:7' for row in rows for b in row))
    async def test_all_info_captions_fit(self):
        for text in (app.WELCOME,app.HELP,app.PRIVACY):self.assertLessEqual(len(text.encode('utf-16-le'))//2,1024)
        self.assertLessEqual(len(app.TERMS.encode('utf-16-le'))//2,4096)
        await app.show_menu(self.m,7);self.assertLessEqual(len(self.m.answer_photo.call_args.kwargs['caption'].encode('utf-16-le'))//2,1024)
        await app.show_pro(self.m,7);self.assertLessEqual(len(self.m.answer_photo.call_args.kwargs['caption'].encode('utf-16-le'))//2,1024)
    async def test_shop_ui_has_all_requested_prices(self):
        await app.show_shop(self.m,7)
        labels=[b.text for row in self.m.answer.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertTrue(any('4 000 000' in x and '10 ⭐' in x for x in labels))
        self.assertTrue(any('25 генераций' in x and '20 ⭐' in x for x in labels))
    async def test_snapshot_backup_new_name(self):
        before=app.db_run('get',7)
        path=app.backup_economy();self.assertTrue(path.is_file());self.assertIn('v40',path.name)
        with closing(sqlite3.connect(path)) as c,c:self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(before,app.db_run('get',7))
    async def test_successful_picture_can_qualify_referral(self):
        ref=await app.eco('register',10);await app.eco('register',7,ref['code'])
        app.db_run('friend_grant',999,7);self.create.return_value=NS(choices=[NS(message=NS(content='ALLOW'))])
        with patch.object(app,'fetch_generated_image',AsyncMock(return_value=b'image')):
            await app.generate_image(self.m,7,'Кот на Луне')
        self.assertEqual((await app.bill('status',10))['balance'],4_500_000)
    async def test_image_http_contract_and_no_url_download(self):
        import httpx,json
        out=BytesIO();Image.new('RGB',(40,40),'red').save(out,format='PNG')
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(out.getvalue()).decode(),'media_type':'image/png'}]})
        original=httpx.AsyncClient
        def local(**kwargs):return original(transport=httpx.MockTransport(handler),**kwargs)
        with patch.object(app.httpx,'AsyncClient',local):result=await app.fetch_generated_image('Кот')
        self.assertTrue(result.startswith(b'\xff\xd8'))
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].method,'POST')
        self.assertEqual(str(calls[0].url),'https://openrouter.ai/api/v1/images')
        self.assertEqual(json.loads(calls[0].content),{'model':app.IMAGE_MODEL,'prompt':'Кот','n':1,'quality':app.IMAGE_QUALITY})
    async def test_image_http_unauthorized_does_not_fallback(self):
        import httpx
        calls=[]
        def handler(request):calls.append(request);return httpx.Response(402,json={'error':'unavailable'})
        original=httpx.AsyncClient
        def local(**kwargs):return original(transport=httpx.MockTransport(handler),**kwargs)
        with patch.object(app.httpx,'AsyncClient',local):
            with self.assertRaises(httpx.HTTPStatusError):await app.fetch_generated_image('Кот')
        self.assertEqual(len(calls),1)
    async def test_ready_images_reencoded_without_metadata(self):
        out=BytesIO();Image.new('RGBA',(30,40),(255,0,0,128)).save(out,format='PNG')
        result=app.safe_image_bytes(out.getvalue());self.assertTrue(result.startswith(b'\xff\xd8'))
        with self.assertRaises((ValueError,OSError)):app.safe_image_bytes(b'<svg></svg>')

class DispatcherIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_complete_two_bot_flow_and_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            main=Bot(token='111111:'+('A'*35),session=LocalSession());pay=Bot(token='222222:'+('B'*35),session=LocalSession())
            create=AsyncMock(return_value=NS(choices=[NS(message=NS(content='Четыре.'),finish_reason='stop')]))
            patches=[patch.object(app,'DB',Path(tmp)/'db'),patch.object(app,'OWNER_ID',999),patch.object(app,'MAIN_BOT',main),patch.object(app,'PAYMENT_BOT',pay),patch.object(app,'MAIN_USERNAME','main_bot'),patch.object(app,'PAYMENT_USERNAME','pay_bot'),patch.object(app,'PAYMENTS_ENABLED',True),patch.object(app,'PAYMENT_BOT_TOKEN','test-value'),patch.object(app,'VOLUME_PATH',tmp),patch.object(app,'client',NS(chat=NS(completions=NS(create=create)))),patch.object(app,'in_flight',0)]
            for k in ('_CHANNEL_ADMIN_UNTIL','_CHANNEL_RETRY_UNTIL','_CHANNEL_SCREENS','_CHANNEL_MEMBERS','_CHANNEL_LAST_ERROR'):patches.append(patch.object(app,k,{}))
            for p in patches:p.start()
            dp=Dispatcher(disable_fsm=True);dp.include_routers(app.router,app.pay_router)
            app.db_init();billing.init(app.DB);economy.init(app.DB)
            seq=0
            async def feed(bot,field,value):
                nonlocal seq
                seq+=1
                return await asyncio.wait_for(dp.feed_update(bot,Update.model_validate({'update_id':seq,field:value})),timeout=10)
            def msg(uid,text=None,extra=None):
                d={'message_id':seq+1,'date':0,'chat':{'id':uid,'type':'private'},'from':{'id':uid,'is_bot':False,'first_name':'Tester'}}
                if text is not None:d['text']=text
                d.update(extra or {});return d
            def cb(uid,data):
                return {'id':str(seq),'from':{'id':uid,'is_bot':False,'first_name':'Tester'},'chat_instance':'ci','data':data,'message':{'message_id':100,'date':0,'chat':{'id':uid,'type':'private'},'from':{'id':main.id,'is_bot':True,'first_name':'Bot'},'text':'Menu'}}
            try:
                # Register inviter; a new invitee starts through their actual deep link.
                await feed(main,'message',msg(10,'/start'));code=(await app.eco('ref_stats',10))['code']
                await feed(main,'message',msg(7,'/start ref_'+code));await feed(main,'message',msg(7,'/agree'))
                await feed(main,'callback_query',cb(7,'mode:smart'))
                await feed(main,'message',msg(7,'Сколько будет два плюс два?'))
                self.assertEqual((await app.bill('status',7))['balance'],2_280_000)
                self.assertEqual((await app.bill('status',10))['balance'],4_500_000)
                # A link is tied to purchaser ID; forwarded starts cannot create invoices.
                await feed(main,'callback_query',cb(7,'shop:tokens'))
                method=[m for n,m in main.session.calls if n=='SendMessage'][-1]
                link=method.reply_markup.inline_keyboard[0][0].url.split('start=')[1]
                await feed(pay,'message',msg(8,'/start '+link));self.assertFalse(any(n=='SendInvoice' for n,_ in pay.session.calls))
                await feed(pay,'message',msg(7,'/start '+link));invoice=[m for n,m in pay.session.calls if n=='SendInvoice'][-1]
                self.assertEqual(invoice.prices[0].amount,10)
                pc={'id':'pc','from':msg(7)['from'],'currency':'XTR','total_amount':10,'invoice_payload':invoice.payload}
                await feed(main,'pre_checkout_query',pc);self.assertFalse([m for n,m in main.session.calls if n=='AnswerPreCheckoutQuery'][-1].ok)
                await feed(pay,'pre_checkout_query',pc);self.assertTrue([m for n,m in pay.session.calls if n=='AnswerPreCheckoutQuery'][-1].ok)
                receipt={'successful_payment':{'currency':'XTR','total_amount':10,'invoice_payload':invoice.payload,'telegram_payment_charge_id':'charge1','provider_payment_charge_id':'provider'}}
                # Maintenance blocks normal users in BOTH bots; Owner can turn it off.
                await feed(main,'message',msg(999,'/agree'))
                await feed(main,'callback_query',cb(999,'maintenance:set:1'))
                await feed(main,'message',msg(7,'/menu'));self.assertEqual([m for n,m in main.session.calls if n=='SendMessage'][-1].text,app.MAINTENANCE_TEXT)
                await feed(pay,'message',msg(7,'/start '+link));self.assertEqual([m for n,m in pay.session.calls if n=='SendMessage'][-1].text,app.MAINTENANCE_TEXT)
                await feed(main,'callback_query',cb(7,'mode:raw'));self.assertEqual(app.db_run('get',7)[1],'smart')
                # Already paid receipts MUST be booked even while the gate is on.
                await feed(pay,'message',msg(7,extra=receipt));await feed(pay,'message',msg(7,extra=receipt))
                self.assertEqual((await app.bill('status',7))['balance'],6_280_000)
                self.assertEqual((await app.bill('status',10))['balance'],4_700_000)
                await feed(pay,'pre_checkout_query',pc);self.assertFalse([m for n,m in pay.session.calls if n=='AnswerPreCheckoutQuery'][-1].ok)
                await feed(main,'callback_query',cb(999,'maintenance:set:0'))
                await feed(main,'message',msg(7,'🪙 Баланс'));self.assertIn('6 280 000',[m for n,m in main.session.calls if n=='SendMessage'][-1].text)
                await feed(main,'message',msg(7,'/delete'));self.assertEqual((await app.bill('status',7))['balance'],6_280_000)
                self.assertTrue((await app.eco('register',7))['rewarded'])
                # Refund reverses both credits and commission; Plus ledger is untouched.
                refunded={'refunded_payment':{'currency':'XTR','total_amount':10,'invoice_payload':invoice.payload,'telegram_payment_charge_id':'charge1','provider_payment_charge_id':'provider'}}
                await feed(pay,'message',msg(7,extra=refunded));self.assertEqual((await app.bill('status',7))['balance'],2_280_000)
                self.assertEqual((await app.bill('status',10))['balance'],4_500_000)
                self.assertFalse(billing.run(app.DB,'status',7)['pro'])
            finally:
                await main.session.close();await pay.session.close();await dp.storage.close()
                # Permit another integration suite in the same process.
                app.router._parent_router=None;app.pay_router._parent_router=None
                for p in reversed(patches):p.stop()

if __name__=='__main__':unittest.main()
