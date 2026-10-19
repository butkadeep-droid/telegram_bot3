from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import billing
import economy as e

class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.db'
        with closing(sqlite3.connect(self.path)) as c,c:
            c.executescript('CREATE TABLE bot_members(user_id INTEGER PRIMARY KEY, username TEXT);CREATE TABLE users(user_id INTEGER PRIMARY KEY);CREATE TABLE bot_bans(user_id INTEGER PRIMARY KEY);')
        billing.init(self.path)
        e.init(self.path)
        self.now=100*86400+100
    def tearDown(self):self.tmp.cleanup()
    def r(self,action,uid=7,v=None,t=None,owner=999):
        return e.run(self.path,action,uid,v,self.now if t is None else t,owner)
    def sql(self,query,args=()):
        with closing(sqlite3.connect(self.path)) as c,c:return c.execute(query,args).fetchall()
    def setbalance(self,amount,uid=7):
        self.r('status',uid)
        self.sql('UPDATE credit_wallets SET balance=? WHERE user_id=?',(amount,uid))
    def friend(self,uid=7):self.sql('INSERT OR REPLACE INTO friend_roles VALUES(?,?,?)',(uid,999,self.now))
    def plus(self,uid=7):self.sql('INSERT OR REPLACE INTO pro_entitlements VALUES(?,?)',(uid,self.now+10*86400))
    def purchase(self,uid=7,product='tokens',charge='charge',t=None):
        t=self.now if t is None else t
        self.r('create_order',uid,dict(payload='gs_link'+charge,merchant_id=22,product=product),t)
        order=self.r('invoice',uid,dict(link='gs_link'+charge,payload='gs_invoice'+charge,merchant_id=22),t)
        amount=order['price']
        self.assertTrue(self.r('precheck',uid,(order['payload'],amount,'XTR',22),t))
        return self.r('payment',uid,(charge,order['payload'],amount,'XTR',22),t)
    def referral(self):
        ref=self.r('register',10)
        self.r('register',7,ref['code'])
    def test_initial_shared_wallet(self):
        s=self.r('status');self.assertEqual(s['balance'],2_500_000);self.assertEqual(s['images'],0)
    def test_modes_use_one_wallet(self):
        a=self.r('reserve',v={});self.r('commit',v=a['reservation'])
        b=self.r('reserve',v={'smart':True},t=self.now+5);self.r('commit',v=b['reservation'])
        self.assertEqual(self.r('status')['balance'],2_180_000)
    def test_daily_topup_not_add(self):
        self.setbalance(1_000_000);self.assertEqual(self.r('status',t=self.now+86400)['balance'],2_500_000)
    def test_no_midday_refill(self):
        self.setbalance(1);self.assertEqual(self.r('status',t=self.now+5)['balance'],1)
    def test_above_target_never_shrinks_or_adds(self):
        self.setbalance(9_000_000)
        for i in (1,2,7):self.assertEqual(self.r('status',t=self.now+i*86400)['balance'],9_000_000)
    def test_exact_target_no_extra(self):self.assertEqual(self.r('status',t=self.now+86400)['balance'],2_500_000)
    def test_boundary_utc_midnight(self):
        self.setbalance(1);self.assertEqual(self.r('status',t=101*86400-1)['balance'],1)
        self.assertEqual(self.r('status',t=101*86400)['balance'],2_500_000)
    def test_plus_upgrade_tops_to_five(self):
        self.setbalance(1);self.plus();self.assertEqual(self.r('status')['balance'],5_000_000)
    def test_plus_upgrade_does_not_shrink(self):
        self.setbalance(9_000_000);self.plus();self.assertEqual(self.r('status')['balance'],9_000_000)
    def test_plus_daily(self):
        self.plus();self.setbalance(1);self.assertEqual(self.r('status',t=self.now+86400)['balance'],5_000_000)
    def test_downgrade_preserves_balance(self):
        self.plus();self.r('status');self.sql('DELETE FROM pro_entitlements');self.assertEqual(self.r('status')['balance'],5_000_000)
    def test_friend_hourly(self):
        self.friend();self.setbalance(5);self.assertEqual(self.r('status',t=self.now+3600)['balance'],20_000_000)
    def test_friend_above_hourly(self):
        self.friend();self.setbalance(25_000_000);self.assertEqual(self.r('status',t=self.now+3600)['balance'],25_000_000)
    def test_owner_unlimited_no_debit_no_cooldown(self):
        for _ in range(30):
            p=self.r('reserve',999,{'smart':True});self.assertTrue(p['ok']);self.assertIsNone(p['reservation'])
        self.assertTrue(self.r('status',999)['owner'])
    def test_friend_cooldown_not_quota_bypass(self):
        self.friend();self.assertTrue(self.r('reserve')['ok']);self.assertFalse(self.r('reserve')['ok'])
        self.setbalance(1);self.assertEqual(self.r('reserve',t=self.now+5)['reason'],'tokens')
    def test_smart_ordinary_access(self):self.assertTrue(self.r('reserve',v={'smart':True})['ok'])
    def test_insufficient_no_debit(self):
        self.setbalance(99_999);self.assertEqual(self.r('reserve')['reason'],'tokens');self.assertEqual(self.r('status')['balance'],99_999)
    def test_failed_answer_refund_once(self):
        p=self.r('reserve');self.assertTrue(self.r('release',v=p['reservation']));self.assertFalse(self.r('release',v=p['reservation']));self.assertEqual(self.r('status')['balance'],2_500_000)
    def test_spent_never_refunded(self):
        p=self.r('reserve');self.r('commit',v=p['reservation']);self.assertFalse(self.r('release',v=p['reservation']))
    def test_other_user_cannot_release(self):
        p=self.r('reserve');self.assertFalse(self.r('release',8,p['reservation']))
    def test_pending_request_across_midnight_does_not_mint_tokens(self):
        p=self.r('reserve');self.assertEqual(self.r('status',t=self.now+86400)['balance'],2_400_000)
        self.r('release',v=p['reservation'],t=self.now+86400);self.assertEqual(self.r('status',t=self.now+86400)['balance'],2_500_000)
    def test_startup_recovery_once(self):
        self.r('reserve');self.assertEqual(self.r('recover',t=self.now+601),1);self.assertEqual(self.r('recover',t=self.now+601),0);self.assertEqual(self.r('status')['balance'],2_500_000)
    def test_crash_during_send_goes_to_review(self):
        p=self.r('reserve');self.r('sending',v=p['reservation']);self.r('recover',t=self.now+601);self.assertEqual(self.r('status')['balance'],2_400_000)
        self.assertEqual(self.sql('SELECT state FROM credit_holds')[0][0],'review')
    def test_concurrent_reservation_is_serialized(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            states=list(pool.map(lambda _:self.r('reserve'),range(12)))
        self.assertEqual(sum(x['ok'] for x in states),1)
    def test_referral_once_after_both_conditions(self):
        self.referral();self.assertIsNone(self.r('qualify',v=True));self.r('first_answer');self.assertIsNone(self.r('qualify',v=False))
        self.assertEqual(self.r('qualify',v=True),10);self.assertIsNone(self.r('qualify',v=True));self.assertEqual(self.r('status',10)['balance'],4_500_000)
    def test_self_referral(self):
        p=self.r('register');self.assertIsNone(self.r('register',v=p['code'])['referrer'])
    def test_existing_user_cannot_bind(self):
        self.r('register');p=self.r('register',10);self.assertIsNone(self.r('register',v=p['code'])['referrer'])
    def test_referrer_cannot_be_replaced(self):
        self.referral();p=self.r('register',11);self.assertEqual(self.r('register',v=p['code'])['referrer'],10)
    def test_unknown_referral_code(self):self.assertIsNone(self.r('register',v='nope')['referrer'])
    def test_old_members_seeded_no_reward(self):
        self.sql('INSERT INTO bot_members VALUES(123,NULL)');self.sql("DELETE FROM credit_meta WHERE key='seeded-v39'");e.init(self.path)
        p=self.r('register',10);self.assertIsNone(self.r('register',123,p['code'])['referrer'])
    def test_restart_does_not_reseed_new_person(self):
        self.referral();e.init(self.path);self.assertEqual(self.r('register')['referrer'],10)
    def test_delete_profile_does_not_clear_referral_award(self):
        self.referral();self.r('first_answer');self.r('qualify',v=True);self.sql('DELETE FROM users');self.sql('DELETE FROM bot_members');self.assertIsNone(self.r('qualify',v=True))
    def test_banned_invitee_no_award(self):
        self.referral();self.r('first_answer');self.sql('INSERT INTO bot_bans VALUES(7)');self.assertIsNone(self.r('qualify',v=True))
    def test_referral_stats(self):
        self.referral();self.r('first_answer');self.r('qualify',v=True);s=self.r('ref_stats',10);self.assertEqual((s['invited'],s['qualified'],s['reward']),(1,1,2_000_000))
    def test_token_purchase_stacks(self):
        self.assertEqual(self.purchase()['state'],'granted');self.assertEqual(self.r('status')['balance'],6_500_000)
        self.purchase(charge='two');self.assertEqual(self.r('status')['balance'],10_500_000)
    def test_token_purchase_commission(self):
        self.referral();self.purchase();self.assertEqual(self.r('status',10)['balance'],2_700_000)
        self.assertEqual(self.r('ref_stats',10)['commission'],200_000)
    def test_image_purchase_no_commission(self):
        self.referral();self.purchase(product='images25');self.assertEqual(self.r('status')['images'],25);self.assertEqual(self.r('status',10)['balance'],2_500_000)
    def test_duplicate_payment_no_double_credit(self):
        self.purchase();r=self.r('payment',v=('charge','gs_invoicecharge',10,'XTR',22));self.assertEqual(r['state'],'duplicate');self.assertEqual(self.r('status')['balance'],6_500_000)
    def test_second_charge_same_invoice_orphan(self):
        self.purchase();r=self.r('payment',v=('other','gs_invoicecharge',10,'XTR',22));self.assertEqual(r['state'],'orphan');self.assertEqual(self.r('status')['balance'],6_500_000)
    def test_checkout_exact_price_merchant_owner_currency(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'))
        self.r('invoice',v=dict(link='gs_l',payload='gs_i',merchant_id=22))
        for uid,amount,cur,merchant in ((8,10,'XTR',22),(7,9,'XTR',22),(7,10,'USD',22),(7,10,'XTR',23)):
            self.assertFalse(self.r('precheck',uid,('gs_i',amount,cur,merchant)))
    def test_link_cannot_be_paid_directly(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'));self.assertFalse(self.r('precheck',v=('gs_l',10,'XTR',22)))
    def test_invoice_link_single_use(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'))
        self.assertIsNotNone(self.r('invoice',v=dict(link='gs_l',payload='gs_i',merchant_id=22)))
        self.assertIsNone(self.r('invoice',v=dict(link='gs_l',payload='gs_i2',merchant_id=22)))
    def test_foreign_link_cannot_be_invoiced(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'));self.assertIsNone(self.r('invoice',8,dict(link='gs_l',payload='gs_i',merchant_id=22)))
    def test_expired_link(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'));self.assertIsNone(self.r('invoice',v=dict(link='gs_l',payload='gs_i',merchant_id=22),t=self.now+1801))
    def test_paid_late_after_checkout_still_granted(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'));self.r('invoice',v=dict(link='gs_l',payload='gs_i',merchant_id=22));self.r('precheck',v=('gs_i',10,'XTR',22))
        self.assertEqual(self.r('payment',v=('late','gs_i',10,'XTR',22),t=self.now+86400)['state'],'granted')
    def test_refund_reverses_buyer_and_referrer_once(self):
        self.referral();self.purchase();self.r('refund',v=('charge',22));self.r('refund',v=('charge',22))
        self.assertEqual(self.r('status')['balance'],2_500_000);self.assertEqual(self.r('status',10)['balance'],2_500_000)
        self.assertEqual(self.r('ref_stats',10)['commission'],0)
    def test_refund_wrong_merchant_rejected(self):
        self.purchase();self.assertFalse(self.r('refund',v=('charge',23)));self.assertEqual(self.r('status')['balance'],6_500_000)
    def test_refund_before_payment_no_grant(self):
        self.r('refund',v=('charge',22));self.assertEqual(self.purchase()['state'],'refunded');self.assertEqual(self.r('status')['balance'],2_500_000)
    def test_spent_refund_creates_debt_future_topups_repay(self):
        self.purchase();self.setbalance(0);self.r('refund',v=('charge',22));self.assertEqual(self.r('status')['debt'],4_000_000)
        s=self.r('status',t=self.now+86400);self.assertEqual((s['balance'],s['debt']),(0,1_500_000))
        s=self.r('status',t=self.now+2*86400);self.assertEqual((s['balance'],s['debt']),(1_000_000,0))
    def test_no_plus_from_token_or_image_purchase(self):
        self.purchase();self.purchase(product='images10',charge='im');self.assertFalse(billing.run(self.path,'status',7,now=self.now)['pro'])
    def test_plus_daily_image(self):
        self.plus();p=self.r('reserve',v={'image':True});self.assertTrue(p['ok']);self.r('commit',v=p['reservation']);self.assertEqual(self.r('reserve',v={'image':True},t=self.now+5)['reason'],'images')
        self.assertTrue(self.r('reserve',v={'image':True},t=self.now+86400)['ok'])
    def test_friend_ten_images_hourly(self):
        self.friend()
        for i in range(10):
            p=self.r('reserve',v={'image':True},t=self.now+i*5);self.assertTrue(p['ok']);self.r('commit',v=p['reservation'])
        self.assertEqual(self.r('reserve',v={'image':True},t=self.now+60)['reason'],'images');self.assertTrue(self.r('reserve',v={'image':True},t=self.now+3600)['ok'])
    def test_ordinary_no_free_images(self):self.assertEqual(self.r('reserve',v={'image':True})['reason'],'images')
    def test_images_free_first_paid_second(self):
        self.plus();self.purchase(product='images10');p=self.r('reserve',v={'image':True});self.r('commit',v=p['reservation']);self.assertEqual(self.r('status')['images'],10)
        p=self.r('reserve',v={'image':True},t=self.now+5);self.r('commit',v=p['reservation']);self.assertEqual(self.r('status')['images'],9)
    def test_images_no_token_debit(self):
        self.purchase(product='images50');p=self.r('reserve',v={'image':True});self.r('commit',v=p['reservation']);self.assertEqual(self.r('status')['balance'],2_500_000)
    def test_image_fail_refunds_paid_once(self):
        self.purchase(product='images10');p=self.r('reserve',v={'image':True});self.r('release',v=p['reservation']);self.r('release',v=p['reservation']);self.assertEqual(self.r('status')['images'],10)
    def test_image_fail_free_old_window_not_new(self):
        self.plus();p=self.r('reserve',v={'image':True});self.r('status',t=self.now+86400);self.r('release',v=p['reservation'],t=self.now+86400);self.assertEqual(self.r('status',t=self.now+86400)['image_free'],1)
    def test_paid_images_persist_daily(self):
        self.purchase(product='images50');self.assertEqual(self.r('status',t=self.now+86400)['images'],50)
    def test_admin_gift_auth_confirm_idempotent(self):
        self.sql('INSERT INTO bot_members VALUES(7,NULL)');self.assertIsNone(self.r('gift_prepare',8,7));token=self.r('gift_prepare',999,7)
        self.assertIsNone(self.r('gift_apply',8,token));self.assertEqual(self.r('gift_apply',999,token),7);self.assertIsNone(self.r('gift_apply',999,token));self.assertEqual(self.r('status')['balance'],3_500_000)
    def test_admin_gift_expiry(self):
        self.sql('INSERT INTO bot_members VALUES(7,NULL)');token=self.r('gift_prepare',999,7);self.assertIsNone(self.r('gift_apply',999,token,t=self.now+601))
    def test_concurrent_payment_grants_once(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'));self.r('invoice',v=dict(link='gs_l',payload='gs_i',merchant_id=22));self.r('precheck',v=('gs_i',10,'XTR',22))
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(lambda _:self.r('payment',v=('same','gs_i',10,'XTR',22)),range(10)))
        self.assertEqual(sum(r['state']=='granted' for r in results),1);self.assertEqual(self.r('status')['balance'],6_500_000)
    def test_rolling_deploy_does_not_release_live_hold(self):
        p=self.r('reserve');self.assertEqual(self.r('recover',t=self.now+100),0)
        self.assertEqual(self.r('status')['balance'],2_400_000);self.assertTrue(self.r('commit',v=p['reservation']))
    def test_image_refund_debt_repaid_by_next_free_allowance(self):
        self.plus();self.purchase(product='images10');self.sql('UPDATE credit_wallets SET images=0,image_used=1 WHERE user_id=7')
        self.r('refund',v=('charge',22));self.assertEqual(self.r('status')['image_debt'],10)
        s=self.r('status',t=self.now+86400);self.assertEqual(s['image_debt'],9);self.assertEqual(s['image_free'],0)
    def test_friend_maintenance_no_exemption(self):
        self.friend();self.assertIsNone(self.r('maintenance_set',7,True));self.assertTrue(self.r('maintenance_set',999,True));self.assertTrue(self.r('maintenance_get'))
    def test_checkout_record_survives_order_cleanup(self):
        self.r('create_order',v=dict(payload='gs_l',merchant_id=22,product='tokens'));self.r('invoice',v=dict(link='gs_l',payload='gs_i',merchant_id=22));self.r('precheck',v=('gs_i',10,'XTR',22))
        self.r('cleanup',t=self.now+30*86400)
        self.assertEqual(self.r('payment',v=('late','gs_i',10,'XTR',22),t=self.now+30*86400)['state'],'granted')
    def test_concurrent_referral_award_once(self):
        self.referral();self.r('first_answer')
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:self.r('qualify',v=True),range(10)))
        self.assertEqual(sum(r==10 for r in results),1);self.assertEqual(self.r('status',10)['balance'],4_500_000)

if __name__=='__main__':unittest.main()
