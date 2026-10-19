import unittest,json,base64
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import httpx
from PIL import Image
import bot as app
import test_runtime_v39 as fixtures

class PremiumTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RuntimeTests.asyncSetUp
    asyncTearDown=fixtures.RuntimeTests.asyncTearDown
    fake_message=fixtures.RuntimeTests.fake_message
    async def test_safety_independent_from_chat_model(self):
        self.create.return_value=NS(choices=[NS(message=NS(content='ALLOW'),finish_reason='stop')])
        with patch.object(app,'MODEL','unrelated/chat'),patch.object(app,'IMAGE_SAFETY_MODEL','openai/gpt-4.1-mini'):
            self.assertEqual(await app.check_image_prompt('лимон'),'ALLOW')
        self.assertEqual(self.create.call_args.kwargs['model'],'openai/gpt-4.1-mini')
        self.assertNotIn('extra_body',self.create.call_args.kwargs)
    async def test_high_one_image_no_retries(self):
        calls=[];out=BytesIO();Image.new('RGB',(32,32)).save(out,format='PNG')
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(out.getvalue()).decode()}]})
        original=httpx.AsyncClient
        def local(**kw):return original(transport=httpx.MockTransport(handler),**kw)
        with patch.object(app,'IMAGE_MODEL','openai/gpt-image-2'),patch.object(app,'IMAGE_QUALITY','high'),patch.object(app.httpx,'AsyncClient',local):
            self.assertTrue((await app.fetch_generated_image('машинка')).startswith(b'\xff\xd8'))
        self.assertEqual(len(calls),1)
        self.assertEqual(json.loads(calls[0].content),{'model':'openai/gpt-image-2','quality':'high','n':1,'prompt':'машинка'})
    async def test_other_model_not_sent_openai_quality(self):
        calls=[]
        def handler(req):calls.append(req);return httpx.Response(402,json={'error':'x'})
        original=httpx.AsyncClient
        def local(**kw):return original(transport=httpx.MockTransport(handler),**kw)
        with patch.object(app,'IMAGE_MODEL','bytedance-seed/seedream-4.5'),patch.object(app.httpx,'AsyncClient',local):
            with self.assertRaises(httpx.HTTPStatusError):await app.fetch_generated_image('кот')
        self.assertNotIn('quality',json.loads(calls[0].content));self.assertEqual(len(calls),1)
    async def test_owner_generation_error_stage_and_status(self):
        self.create.return_value=NS(choices=[NS(message=NS(content='ALLOW'))])
        req=httpx.Request('POST','https://example.com')
        err=httpx.HTTPStatusError('private response body',request=req,response=httpx.Response(402,request=req))
        msg=self.fake_message(999)
        with patch.object(app,'fetch_generated_image',AsyncMock(side_effect=err)):
            await app.generate_image(msg,999,'лимон')
        text=msg.answer.call_args.args[0]
        self.assertIn('generation',text);self.assertIn('402',text);self.assertNotIn('private response body',text)
    async def test_owner_safety_error_stage(self):
        req=httpx.Request('POST','https://example.com')
        self.create.side_effect=httpx.HTTPStatusError('secret',request=req,response=httpx.Response(403,request=req))
        msg=self.fake_message(999)
        with patch.object(app,'fetch_generated_image',AsyncMock()) as fetch:
            await app.generate_image(msg,999,'лимон')
        fetch.assert_not_awaited();self.assertIn('safety',msg.answer.call_args.args[0]);self.assertIn('403',msg.answer.call_args.args[0])
    async def test_user_failure_short_and_refunded(self):
        app.db_run('friend_grant',999,7)
        self.create.return_value=NS(choices=[NS(message=NS(content='ALLOW'))])
        with patch.object(app,'fetch_generated_image',AsyncMock(side_effect=ValueError('private'))):
            await app.generate_image(self.m,7,'лимон')
        self.assertEqual(self.m.answer.call_args.args[0],'🎨 Генерация пока недоступна.')
        self.assertEqual((await app.bill('status',7))['image_free'],10)
    async def test_no_classifier_fallback_to_conversation(self):
        self.create.return_value=NS(choices=[NS(message=NS(content=None),finish_reason='length')])
        self.assertEqual(await app.check_image_prompt('лимон'),'UNKNOWN')
        self.assertEqual(self.create.await_count,2)
        self.assertTrue(all(c.kwargs['model']==app.IMAGE_SAFETY_MODEL for c in self.create.call_args_list))

if __name__=='__main__':unittest.main()
