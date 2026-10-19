import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import bot as app
import test_runtime_v39 as fixtures

def reply(text,finish='stop'):
    return NS(choices=[NS(message=NS(content=text),finish_reason=finish)])

class VerdictTests(unittest.TestCase):
    def test_explicit_verdicts(self):
        for text in ('ALLOW','allow','\nALLOW\n','```\nALLOW\n```'):
            with self.subTest(text=text):self.assertEqual(app.parse_image_verdict(reply(text)),'ALLOW')
    def test_no_substring_approval(self):
        for text in ('not ALLOW','ALLOW BLOCK','sure ALLOW maybe','',None,'{"decision":"ALLOW"}'):
            with self.subTest(text=text):self.assertEqual(app.parse_image_verdict(reply(text)),'UNKNOWN')
    def test_truncated_allow_is_not_approved(self):
        self.assertEqual(app.parse_image_verdict(reply('ALLOW','length')),'UNKNOWN')
    def test_filter_finish_is_not_approved(self):
        self.assertEqual(app.parse_image_verdict(reply('ALLOW','content_filter')),'UNKNOWN')
    def test_empty_choices(self):
        self.assertEqual(app.parse_image_verdict(NS(choices=[])),'UNKNOWN')
    def test_block(self):self.assertEqual(app.parse_image_verdict(reply('BLOCK')),'BLOCK')

class ImageFixTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RuntimeTests.asyncSetUp
    asyncTearDown=fixtures.RuntimeTests.asyncTearDown
    fake_message=fixtures.RuntimeTests.fake_message
    async def test_lemon_delivered(self):
        app.db_run('friend_grant',999,7)
        self.create.return_value=reply('ALLOW')
        with patch.object(app,'fetch_generated_image',AsyncMock(return_value=b'image')) as fetch:
            await app.generate_image(self.m,7,'лимон')
        fetch.assert_awaited_once_with('лимон');self.m.answer_photo.assert_awaited_once()
        self.assertEqual(self.create.call_args.kwargs['max_tokens'],256)
        self.assertEqual(self.create.call_args.kwargs['extra_body'],{'reasoning':{'enabled':False}})
        self.assertEqual((await app.bill('status',7))['image_free'],9)
    async def test_car_delivered(self):
        app.db_run('friend_grant',999,7);self.create.return_value=reply('allow')
        with patch.object(app,'fetch_generated_image',AsyncMock(return_value=b'image')) as fetch:
            await app.generate_image(self.m,7,'сгенерируй мне машинку')
        fetch.assert_awaited_once();self.m.answer_photo.assert_awaited_once()
    async def test_empty_then_allow(self):
        self.create.side_effect=[reply(None,'length'),reply('ALLOW')]
        self.assertEqual(await app.check_image_prompt('лимон'),'ALLOW')
        self.assertEqual([c.kwargs['max_tokens'] for c in self.create.call_args_list],[256,512])
    async def test_block_not_retried(self):
        self.create.return_value=reply('BLOCK')
        self.assertEqual(await app.check_image_prompt('описание'),'BLOCK');self.create.assert_awaited_once()
    async def test_invalid_twice_not_false_accusation_or_charge(self):
        app.db_run('friend_grant',999,7);self.create.return_value=reply(None,'length')
        with patch.object(app,'fetch_generated_image',AsyncMock()) as fetch:
            await app.generate_image(self.m,7,'лимон')
        fetch.assert_not_awaited();self.assertEqual(self.create.await_count,2)
        self.assertIn('пока недоступна',self.m.answer.call_args.args[0])
        self.assertNotIn('не выполню',self.m.answer.call_args.args[0])
        self.assertEqual((await app.bill('status',7))['image_free'],10)
    async def test_block_refunds(self):
        app.db_run('friend_grant',999,7);self.create.return_value=reply('BLOCK')
        with patch.object(app,'fetch_generated_image',AsyncMock()) as fetch:
            await app.generate_image(self.m,7,'описание')
        fetch.assert_not_awaited();self.assertIn('не выполню',self.m.answer.call_args.args[0])
        self.assertEqual((await app.bill('status',7))['image_free'],10)
    async def test_logs_do_not_include_user_prompt(self):
        self.create.return_value=reply('ALLOW')
        with self.assertLogs(level='WARNING') as logs:await app.check_image_prompt('private prompt xyz')
        self.assertNotIn('private prompt xyz',' '.join(logs.output))
        self.assertIn('verdict=ALLOW',' '.join(logs.output))

if __name__=='__main__':unittest.main()
