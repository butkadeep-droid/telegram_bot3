"""8m outstanding INVITATION credits only; 5% commissions/purchases uncapped."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import unittest
import economy as e
import test_economy_v39 as fixture

class ReferralCapTests(unittest.TestCase):
    setUp=fixture.EconomyTests.setUp
    tearDown=fixture.EconomyTests.tearDown
    r=fixture.EconomyTests.r
    sql=fixture.EconomyTests.sql
    purchase=fixture.EconomyTests.purchase
    friend=fixture.EconomyTests.friend
    plus=fixture.EconomyTests.plus
    def candidate(self,index):
        ref=self.r('register',10)
        uid=100+index
        self.r('register',uid,ref['code']);self.r('first_answer',uid)
        return uid
    def reward(self,index):return self.r('qualify',self.candidate(index),True)
    def fill(self):
        for i in range(4):self.assertEqual(self.reward(i),10)
    def spend(self,number,smart=False,commit=True):
        holds=[]
        for _ in range(number):
            tick=getattr(self,'tick',0);self.tick=tick+5
            p=self.r('reserve',10,{'smart':smart},self.now+tick)
            self.assertTrue(p['ok']);holds.append(p['reservation'])
            if commit:self.r('commit',10,p['reservation'])
        return holds
    def state(self):return self.r('status',10)
    def test_four_rewards_full_eight_million(self):
        self.fill();s=self.state();self.assertEqual(s['referral_balance'],8_000_000)
        self.assertEqual(s['balance'],10_500_000);self.assertEqual(s['referral_slots'],0)
    def test_fifth_not_credited_not_marked_paid(self):
        self.fill();self.assertIsNone(self.reward(4));self.assertEqual(self.state()['balance'],10_500_000)
        self.assertFalse(self.r('register',104)['rewarded'])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM credit_events WHERE kind='referral'")[0][0],4)
    def test_new_reward_after_spending_two_million(self):
        self.fill();self.spend(20);self.assertEqual(self.state()['referral_balance'],6_000_000)
        self.assertEqual(self.state()['referral_slots'],1);self.assertEqual(self.reward(4),10)
        self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_no_lifetime_cap_after_all_eight_spent(self):
        self.fill();self.spend(80);self.assertEqual(self.state()['referral_total'],0)
        for i in range(4,8):self.assertEqual(self.reward(i),10)
        self.assertEqual(self.r('ref_stats',10)['reward'],16_000_000)
        self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_partial_space_not_partial_two_million_reward(self):
        self.fill();self.spend(1);self.assertEqual(self.state()['referral_total'],7_900_000)
        self.assertIsNone(self.reward(4));self.assertEqual(self.state()['referral_total'],7_900_000)
    def test_invitation_funds_spent_first_not_purchase(self):
        self.fill();self.purchase(10,charge='buy');self.spend(20)
        s=self.state();self.assertEqual(s['referral_balance'],6_000_000)
        self.assertEqual(s['balance']-s['referral_balance'],6_500_000)
    def test_purchases_unlimited_while_referral_full(self):
        self.fill()
        for i in range(4):self.purchase(10,charge='buy'+str(i))
        self.assertEqual(self.state()['balance'],26_500_000);self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_five_percent_uncapped_while_referral_full(self):
        self.fill();ref=self.r('register',10);self.r('register',7,ref['code'])
        for i in range(5):self.purchase(7,charge='buy'+str(i))
        self.assertEqual(self.state()['balance'],11_500_000);self.assertEqual(self.state()['referral_total'],8_000_000)
        self.assertEqual(self.r('ref_stats',10)['commission'],1_000_000)
    def test_commission_alone_can_exceed_eight_million(self):
        ref=self.r('register',10);self.r('register',7,ref['code'])
        for i in range(41):self.purchase(7,charge=str(i))
        self.assertEqual(self.r('ref_stats',10)['commission'],8_200_000)
        self.assertEqual(self.state()['referral_total'],0);self.assertEqual(self.state()['referral_slots'],4)
    def test_pending_generation_does_not_open_reward_slot(self):
        self.fill();holds=self.spend(20,commit=False)
        self.assertEqual(self.state()['referral_balance'],6_000_000)
        self.assertEqual(self.state()['referral_reserved'],2_000_000)
        self.assertIsNone(self.reward(4))
        for hold in holds:self.r('commit',10,hold)
        self.assertEqual(self.reward(4),10)
    def test_failed_answer_restores_invitation_funds_once(self):
        self.fill();h=self.spend(1,commit=False)[0]
        self.r('release',10,h);self.r('release',10,h)
        self.assertEqual(self.state()['referral_balance'],8_000_000)
        self.assertEqual(self.state()['balance'],10_500_000);self.assertIsNone(self.reward(4))
    def test_smart_cost_spends_invitation_credits(self):
        self.fill();self.spend(1,smart=True)
        self.assertEqual(self.state()['referral_balance'],7_780_000)
    def test_image_does_not_free_invitation_slots(self):
        self.fill();self.friend(10);self.r('status',10)
        h=self.r('reserve',10,{'image':True})['reservation'];self.r('commit',10,h)
        self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_daily_topup_does_not_reset_referral_source(self):
        self.reward(0);self.assertEqual(self.r('status',10,t=self.now+86400)['referral_balance'],2_000_000)
    def test_role_change_does_not_reset_cap(self):
        self.fill();self.friend(10);self.assertEqual(self.state()['referral_total'],8_000_000)
        self.assertIsNone(self.reward(4));self.sql('DELETE FROM friend_roles WHERE user_id=10')
        self.plus(10);self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_restart_does_not_reset_source(self):
        self.fill();self.spend(20);e.init(self.path)
        self.assertEqual(self.state()['referral_balance'],6_000_000)
    def test_delete_profile_does_not_reset_source_or_duplicates(self):
        self.fill();self.sql('DELETE FROM users');self.sql('DELETE FROM bot_members')
        e.init(self.path);self.assertEqual(self.state()['referral_total'],8_000_000)
        self.assertIsNone(self.reward(0));self.assertIsNone(self.reward(4))
    def test_admin_tokens_not_counted_as_invitation(self):
        self.fill();self.sql('INSERT INTO bot_members VALUES(10,NULL)')
        token=self.r('gift_prepare',999,10);self.r('gift_apply',999,token)
        self.assertEqual(self.state()['balance'],11_500_000);self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_purchase_refund_preserves_invitation_source_when_ordinary_funds_exist(self):
        self.fill();self.purchase(10,charge='buy');self.r('refund',10,('buy',22))
        self.assertEqual(self.state()['balance'],10_500_000);self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_commission_refund_does_not_take_invitation_funds_first(self):
        self.fill();ref=self.r('register',10);self.r('register',7,ref['code']);self.purchase()
        self.r('refund',7,('charge',22));self.assertEqual(self.state()['referral_total'],8_000_000)
        self.assertEqual(self.state()['balance'],10_500_000)
    def test_retry_of_unpaid_referral_can_qualify_when_room_available(self):
        self.fill();self.assertIsNone(self.reward(4));self.spend(20)
        self.assertEqual(self.r('qualify',104,True),10);self.assertIsNone(self.r('qualify',104,True))
    def test_open_slot_is_not_auto_filled_by_waiting_people(self):
        self.fill();self.reward(4);self.spend(20)
        self.assertEqual(self.state()['referral_total'],6_000_000)
        self.assertEqual(self.r('ref_stats',10)['qualified'],4)
    def test_concurrent_new_invitees_cannot_overrun_eight_million(self):
        people=[self.candidate(i) for i in range(12)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda uid:self.r('qualify',uid,True),people))
        self.assertEqual(sum(x==10 for x in results),4);self.assertEqual(self.state()['referral_total'],8_000_000)
    def test_crash_recovery_restores_pool_without_overcap(self):
        self.fill();self.spend(20,commit=False);self.assertIsNone(self.reward(4))
        self.r('recover',t=self.now+1000);self.assertEqual(self.state()['referral_total'],8_000_000)
        self.assertEqual(self.state()['balance'],10_500_000)

class MigrationTests(unittest.TestCase):
    setUp=fixture.EconomyTests.setUp
    tearDown=fixture.EconomyTests.tearDown
    r=fixture.EconomyTests.r
    sql=fixture.EconomyTests.sql
    def old_database(self,rewards=4,spent=0,state='spent'):
        self.r('status',10)
        self.sql("DELETE FROM credit_meta WHERE key='ref-sources-v40'")
        self.sql('ALTER TABLE credit_wallets DROP COLUMN referral_balance')
        self.sql('ALTER TABLE credit_holds DROP COLUMN referral_amount')
        for i in range(rewards):self.sql('INSERT INTO credit_events VALUES(?,?,?,?,?)',(f'ref:{100+i}',10,'referral',2_000_000,self.now+i))
        if spent:self.sql('INSERT INTO credit_holds(token,user_id,kind,amount,period,created,state) VALUES(?,?,?,?,?,?,?)',('old',10,'tokens',spent,'old',self.now+10,state))
        total=2_500_000+rewards*2_000_000-(spent if state!='released' else 0)
        self.sql('UPDATE credit_wallets SET balance=? WHERE user_id=10',(total,))
        return total
    def test_migrate_old_eight_million_no_balance_loss(self):
        total=self.old_database();e.init(self.path);s=self.r('status',10)
        self.assertEqual(s['balance'],total);self.assertEqual(s['referral_balance'],8_000_000)
    def test_replay_past_spending_referral_first(self):
        total=self.old_database(spent=2_000_000);e.init(self.path);s=self.r('status',10)
        self.assertEqual(s['balance'],total);self.assertEqual(s['referral_balance'],6_000_000)
    def test_old_released_requests_not_counted_as_spent(self):
        total=self.old_database(spent=2_000_000,state='released');e.init(self.path);s=self.r('status',10)
        self.assertEqual(s['balance'],total);self.assertEqual(s['referral_balance'],8_000_000)
    def test_old_pending_requests_get_source_for_refund(self):
        total=self.old_database(spent=2_000_000,state='held');e.init(self.path)
        s=self.r('status',10);self.assertEqual(s['referral_balance'],6_000_000);self.assertEqual(s['referral_total'],8_000_000)
        self.r('release',10,'old');self.assertEqual(self.r('status',10)['referral_balance'],8_000_000)
        self.assertEqual(self.r('status',10)['balance'],total+2_000_000)
    def test_legacy_overcap_not_confiscated(self):
        total=self.old_database(rewards=5);e.init(self.path);s=self.r('status',10)
        self.assertEqual(s['balance'],total);self.assertEqual(s['referral_balance'],10_000_000);self.assertEqual(s['referral_slots'],0)
    def test_migration_never_attributes_more_than_actual_balance(self):
        self.old_database();self.sql('UPDATE credit_wallets SET balance=100000 WHERE user_id=10')
        e.init(self.path);self.assertEqual(self.r('status',10)['referral_balance'],100_000)
    def test_migration_does_not_replay_twice(self):
        self.old_database();e.init(self.path)
        h=self.r('reserve',10)['reservation'];self.r('commit',10,h);e.init(self.path)
        self.assertEqual(self.r('status',10)['referral_balance'],7_900_000)

if __name__=='__main__':unittest.main()
