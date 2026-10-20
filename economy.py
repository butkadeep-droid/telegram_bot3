"""Transactional virtual credits. No network calls; amounts are NOT model tokens."""
import secrets
import sqlite3
import time
from contextlib import closing

FREE = 2_500_000
PLUS = 5_000_000
FRIEND = 20_000_000
NORMAL_COST = 25_000
SMART_COST = 75_000
REFERRAL_REWARD = 2_000_000
REFERRAL_CAP = 8_000_000
CATALOG = {
    'tokens': ('🪙 4 000 000 токенов', 'tokens', 4_000_000, 10),
    'images10': ('🎨 10 генераций', 'images', 10, 10),
    'images25': ('🎨 25 генераций', 'images', 25, 20),
    'images50': ('🎨 50 генераций', 'images', 50, 35),
}
TTL = 1800


def connect(path):
    con = sqlite3.connect(str(path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=15000')
    return con


def init(path):
    with closing(connect(path)) as con, con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS credit_people(
          user_id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, first_seen REAL NOT NULL,
          referrer INTEGER, first_answer INTEGER NOT NULL DEFAULT 0, rewarded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS credit_wallets(
          user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL, images INTEGER NOT NULL DEFAULT 0,
          token_period TEXT NOT NULL, token_target INTEGER NOT NULL,
          image_period TEXT NOT NULL, image_used INTEGER NOT NULL DEFAULT 0, image_target INTEGER NOT NULL DEFAULT 0,
          debt INTEGER NOT NULL DEFAULT 0, image_debt INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS credit_holds(
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
          amount INTEGER NOT NULL, period TEXT NOT NULL, created REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'held');
        CREATE INDEX IF NOT EXISTS credit_holds_uid ON credit_holds(user_id,state);
        CREATE TABLE IF NOT EXISTS credit_events(
          event_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
          amount INTEGER NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS credit_rates(user_id INTEGER PRIMARY KEY, last_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS credit_admin_gifts(
          token TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS shop_orders(
          payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL, merchant_id INTEGER NOT NULL,
          kind TEXT NOT NULL, product TEXT NOT NULL, quantity INTEGER NOT NULL, price INTEGER NOT NULL,
          created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'new', accepted_terms INTEGER NOT NULL DEFAULT 1,
          invoice_payload TEXT);
        CREATE TABLE IF NOT EXISTS shop_charges(
          charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL, user_id INTEGER NOT NULL,
          amount INTEGER NOT NULL, currency TEXT NOT NULL, merchant_id INTEGER NOT NULL,
          product TEXT NOT NULL, quantity INTEGER NOT NULL, paid_at REAL NOT NULL,
          state TEXT NOT NULL, referrer INTEGER, commission INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS shop_refunds(
          charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, merchant_id INTEGER NOT NULL, seen_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS credit_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        # Preserve pre-migration identity even if /delete later removes the normal profile.
        con.execute('BEGIN IMMEDIATE')
        columns={r[1] for r in con.execute('PRAGMA table_info(credit_wallets)')}
        if 'image_target' not in columns:
            con.execute('ALTER TABLE credit_wallets ADD COLUMN image_target INTEGER NOT NULL DEFAULT 0')
        if 'referral_balance' not in {r[1] for r in con.execute('PRAGMA table_info(credit_wallets)')}:
            con.execute('ALTER TABLE credit_wallets ADD COLUMN referral_balance INTEGER NOT NULL DEFAULT 0')
        if 'referral_amount' not in {r[1] for r in con.execute('PRAGMA table_info(credit_holds)')}:
            con.execute('ALTER TABLE credit_holds ADD COLUMN referral_amount INTEGER NOT NULL DEFAULT 0')
        if not con.execute("SELECT 1 FROM credit_meta WHERE key='ref-sources-v40'").fetchone():
            migrate_referral_sources(con)
            con.execute("INSERT INTO credit_meta VALUES('ref-sources-v40','1')")
        if not con.execute("SELECT 1 FROM credit_meta WHERE key='seeded-v39'").fetchone():
            ids = set()
            for table in ('bot_members', 'users', 'pro_entitlements', 'pro_charges', 'pro_gifts', 'friend_roles', 'bot_bans'):
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    ids.update(r[0] for r in con.execute(f'SELECT user_id FROM {table}'))
            for uid in ids:
                person(con, uid, time.time())
            con.execute("INSERT INTO credit_meta VALUES('seeded-v39','1')")


def migrate_referral_sources(con):
    """No wallet total is modified. Replay the new spend-first rule on v39's ledger.

    Old releases are excluded (no net spend). Legacy refunds/top-ups were not a
    source ledger, so inferred invitation credits are bounded by actual funds.
    Already earned amounts above the new cap are kept, never confiscated.
    """
    pool={}
    rows=con.execute("""SELECT created AS ts,0 AS tie,event_key AS item,user_id,amount,'reward' AS state
       FROM credit_events WHERE kind='referral'
       UNION ALL SELECT created,1,token,user_id,amount,state
       FROM credit_holds WHERE kind='tokens' AND state!='released'
       ORDER BY ts,tie,item""").fetchall()
    for r in rows:
        uid=r['user_id']
        if r['state']=='reward':
            pool[uid]=pool.get(uid,0)+r['amount']
        else:
            used=min(pool.get(uid,0),r['amount'])
            pool[uid]=pool.get(uid,0)-used
            if r['state'] in ('held','sending'):
                con.execute('UPDATE credit_holds SET referral_amount=? WHERE token=?',(used,r['item']))
    for r in con.execute('SELECT user_id,balance FROM credit_wallets').fetchall():
        remaining=min(max(0,r['balance']),pool.get(r['user_id'],0))
        con.execute('UPDATE credit_wallets SET referral_balance=? WHERE user_id=?',(remaining,r['user_id']))


def referral_total(con,uid):
    r=con.execute('SELECT referral_balance FROM credit_wallets WHERE user_id=?',(uid,)).fetchone()
    pending=con.execute("SELECT COALESCE(SUM(referral_amount),0) FROM credit_holds WHERE user_id=? AND state IN ('held','sending')",(uid,)).fetchone()[0]
    return (r[0] if r else 0)+pending


def person(con, uid, now, ref_code=None):
    existing = con.execute('SELECT * FROM credit_people WHERE user_id=?', (uid,)).fetchone()
    if existing:
        return dict(existing)
    ref = con.execute('SELECT user_id FROM credit_people WHERE code=?', (ref_code,)).fetchone() if ref_code else None
    referrer = ref[0] if ref and ref[0] != uid else None
    # The first main-bot interaction fixes attribution. Existing users cannot re-bind.
    con.execute('INSERT INTO credit_people(user_id,code,first_seen,referrer) VALUES(?,?,?,?)',
                (uid, secrets.token_urlsafe(12), now, referrer))
    return dict(con.execute('SELECT * FROM credit_people WHERE user_id=?', (uid,)).fetchone())


def role(con, uid, now, owner):
    friend = con.execute('SELECT 1 FROM friend_roles WHERE user_id=?', (uid,)).fetchone() is not None
    row = con.execute('SELECT until_ts FROM pro_entitlements WHERE user_id=?', (uid,)).fetchone()
    until = row[0] if row else 0
    own = owner > 0 and uid == owner
    span = 3600 if friend else 86400
    cap = FRIEND if friend else PLUS if until > now else FREE
    period = ('h:' if friend else 'd:') + str(int(now // span))
    image_period = ('f:' if friend else 'p:') + str(int(now // span))
    image_cap = 10 if friend else 1 if until > now else 0
    return dict(owner=own, friend=friend, pro=until > now, until=until, target=cap,
                period=period, image_period=image_period, image_cap=image_cap,
                next_refill=(int(now // span)+1)*span)


def credit(con, uid, kind, amount, key, now, owner):
    if con.execute('SELECT 1 FROM credit_events WHERE event_key=?',(key,)).fetchone():
        return False
    wallet(con, uid, now, owner)
    # Only invitation rewards are capped. Purchases, 5% commissions and admin
    # gifts are ordinary funds and do not affect this ceiling.
    if kind=='referral' and referral_total(con,uid)+amount>REFERRAL_CAP:
        return False
    con.execute('INSERT INTO credit_events VALUES(?,?,?,?,?)',(key,uid,kind,amount,now))
    col, debtcol = ('images','image_debt') if kind == 'images' else ('balance','debt')
    w = con.execute(f'SELECT {debtcol} FROM credit_wallets WHERE user_id=?', (uid,)).fetchone()
    repay = min(w[0], amount)
    con.execute(f'UPDATE credit_wallets SET {col}={col}+?, {debtcol}={debtcol}-? WHERE user_id=?',
                (amount-repay,repay,uid))
    if kind=='referral':
        con.execute('UPDATE credit_wallets SET referral_balance=referral_balance+? WHERE user_id=?',(amount-repay,uid))
    return True


def debit_refund(con, uid, kind, amount, now, owner):
    wallet(con,uid,now,owner)
    col,debtcol = ('images','image_debt') if kind == 'images' else ('balance','debt')
    available = con.execute(f'SELECT {col} FROM credit_wallets WHERE user_id=?',(uid,)).fetchone()[0]
    take = min(available,amount)
    if kind!='images':
        refs=con.execute('SELECT referral_balance FROM credit_wallets WHERE user_id=?',(uid,)).fetchone()[0]
        # Reverse ordinary purchase/commission funds first, not invitation funds.
        from_refs=max(0,take-max(0,available-refs))
        con.execute('UPDATE credit_wallets SET referral_balance=referral_balance-? WHERE user_id=?',(from_refs,uid))
    con.execute(f'UPDATE credit_wallets SET {col}={col}-?,{debtcol}={debtcol}+? WHERE user_id=?',
                (take,amount-take,uid))


def wallet(con, uid, now, owner):
    s = role(con,uid,now,owner)
    con.execute('INSERT OR IGNORE INTO credit_wallets(user_id,balance,token_period,token_target,image_period) VALUES(?,?,?,?,?)',
                (uid,s['target'],s['period'],s['target'],s['image_period']))
    w = dict(con.execute('SELECT * FROM credit_wallets WHERE user_id=?',(uid,)).fetchone())
    # Outstanding holds count as balance for refill: crossing midnight during a
    # request and then refunding it must not manufacture extra daily credits.
    if w['token_period'] != s['period'] or s['target'] > w['token_target']:
        held = con.execute("SELECT COALESCE(SUM(amount),0) FROM credit_holds WHERE user_id=? AND kind='tokens' AND state IN ('held','sending')",(uid,)).fetchone()[0]
        topup = max(0,s['target']-w['balance']-held)
        repay = min(w['debt'],topup)
        con.execute('UPDATE credit_wallets SET balance=balance+?,debt=debt-?,token_period=?,token_target=? WHERE user_id=?',
                    (topup-repay,repay,s['period'],s['target'],uid))
    elif w['token_target'] != s['target']:
        con.execute('UPDATE credit_wallets SET token_target=? WHERE user_id=?',(s['target'],uid))
    if w['image_period'] != s['image_period'] or s['image_cap'] > w['image_target']:
        used=0 if w['image_period'] != s['image_period'] else w['image_used']
        repay=min(w['image_debt'],max(0,s['image_cap']-used))
        con.execute('UPDATE credit_wallets SET image_period=?,image_used=?,image_debt=image_debt-?,image_target=? WHERE user_id=?',
                    (s['image_period'],used+repay,repay,s['image_cap'],uid))
    elif w['image_target'] != s['image_cap']:
        con.execute('UPDATE credit_wallets SET image_target=? WHERE user_id=?',(s['image_cap'],uid))
    w = dict(con.execute('SELECT * FROM credit_wallets WHERE user_id=?',(uid,)).fetchone())
    s.update(w)
    s['image_free'] = max(0,s['image_cap']-w['image_used'])
    s['referral_total']=referral_total(con,uid)
    s['referral_limit']=REFERRAL_CAP
    s['referral_reserved']=s['referral_total']-s['referral_balance']
    s['referral_slots']=max(0,(REFERRAL_CAP-s['referral_total'])//REFERRAL_REWARD)
    # Compatibility keys for unchanged diagnostic/role handlers. No old quota enforced.
    s.update(limit=s['target'], remaining=w['balance'],used=0,bonus=0,
             smart_remaining=w['balance']//SMART_COST,smart_limit=s['target']//SMART_COST,
             smart_used=0,next_slot=s['next_refill'])
    return s


def release(con, row, now, owner):
    if not row or row['state'] not in ('held','sending'):
        return False
    con.execute("UPDATE credit_holds SET state='released' WHERE token=?",(row['token'],))
    if row['kind'] == 'free_image':
        con.execute('UPDATE credit_wallets SET image_used=MAX(0,image_used-1) WHERE user_id=? AND image_period=?',
                    (row['user_id'],row['period']))
    else:
        col, debtcol = ('images','image_debt') if row['kind']=='images' else ('balance','debt')
        debt = con.execute(f'SELECT {debtcol} FROM credit_wallets WHERE user_id=?',(row['user_id'],)).fetchone()[0]
        repay = min(row['amount'],debt)
        con.execute(f'UPDATE credit_wallets SET {col}={col}+?,{debtcol}={debtcol}-? WHERE user_id=?',
                    (row['amount']-repay,repay,row['user_id']))
        if row['kind']=='tokens':
            restored=min(row['referral_amount'],row['amount']-repay)
            con.execute('UPDATE credit_wallets SET referral_balance=referral_balance+? WHERE user_id=?',(restored,row['user_id']))
    return True


def run(path, action, uid=0, value=None, now=None, owner=0):
    now = time.time() if now is None else float(now)
    with closing(connect(path)) as con, con:
        con.execute('BEGIN IMMEDIATE')
        if action == 'maintenance_get':
            row=con.execute("SELECT value FROM credit_meta WHERE key='maintenance'").fetchone()
            return bool(row and row[0]=='1')
        if action == 'maintenance_set':
            if not owner or uid!=owner:
                return None
            enabled=bool(value)
            con.execute("INSERT INTO credit_meta VALUES('maintenance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('1' if enabled else '0',))
            return enabled
        if action == 'task_review':
            if not owner or uid != owner or not isinstance(value,dict):return None
            tid,bid=value.get('tid'),value.get('bid')
            r=con.execute("SELECT * FROM task_submissions WHERE id=? AND bot_id=? AND status='pending'",(tid,bid)).fetchone()
            if not r:return None
            if con.execute('SELECT 1 FROM bot_bans WHERE user_id=?',(r['user_id'],)).fetchone():return None
            if value.get('approve'):
                count=con.execute("SELECT COUNT(*) FROM task_evidence WHERE submission_id=? AND state='sent'",(tid,)).fetchone()[0]
                if count!=20:return None
                if con.execute("SELECT 1 FROM task_submissions WHERE user_id=? AND campaign=? AND status='approved'",(r['user_id'],r['campaign'])).fetchone():return None
                key='task:'+r['campaign']+':'+str(r['user_id'])
                if not credit(con,r['user_id'],'task',5_000_000,key,now,owner):return None
                state='approved'
            else:state='rejected'
            con.execute('UPDATE task_submissions SET status=?,updated=? WHERE id=?',(state,now,tid))
            con.execute('DELETE FROM task_sessions WHERE submission_id=?',(tid,))
            return r['user_id']
        if action == 'register':
            return person(con,uid,now,value)
        if action == 'status':
            return wallet(con,uid,now,owner)
        if action == 'reserve':
            v = value or {}
            s = wallet(con,uid,now,owner)
            cooldown = 0 if s['owner'] else 5
            last = con.execute('SELECT last_ts FROM credit_rates WHERE user_id=?',(uid,)).fetchone()
            if last and now-last[0]<cooldown and not v.get('voice_handoff'):
                return dict(ok=False,reason='cooldown',cooldown=cooldown)
            kind = 'tokens'
            amount = SMART_COST if v.get('smart') else NORMAL_COST
            period = s['period']
            if v.get('image'):
                if s['image_debt']:
                    return dict(ok=False,reason='image_debt')
                if s['image_free']:
                    kind,amount,period='free_image',1,s['image_period']
                elif s['images']:
                    kind,amount='images',1
                elif not s['owner']:
                    return dict(ok=False,reason='images',next_refill=s['next_refill'])
            elif not s['owner'] and (s['debt'] or s['balance']<amount):
                return dict(ok=False,reason='tokens',balance=s['balance'],cost=amount,next_refill=s['next_refill'])
            if s['owner']:
                return dict(ok=True,reservation=None)
            token=secrets.token_hex(16)
            con.execute('INSERT INTO credit_rates VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_ts=excluded.last_ts',(uid,now))
            ref_part=min(amount,s['referral_balance']) if kind=='tokens' else 0
            if kind=='free_image':
                con.execute('UPDATE credit_wallets SET image_used=image_used+1 WHERE user_id=?',(uid,))
            else:
                col='images' if kind=='images' else 'balance'
                con.execute(f'UPDATE credit_wallets SET {col}={col}-? WHERE user_id=?',(amount,uid))
            if ref_part:
                con.execute('UPDATE credit_wallets SET referral_balance=referral_balance-? WHERE user_id=?',(ref_part,uid))
            con.execute('INSERT INTO credit_holds(token,user_id,kind,amount,period,created,referral_amount) VALUES(?,?,?,?,?,?,?)',
                        (token,uid,kind,amount,period,now,ref_part))
            return dict(ok=True,reservation=token)
        if action in ('release','commit','sending'):
            row=con.execute('SELECT * FROM credit_holds WHERE token=? AND user_id=?',(value,uid)).fetchone()
            if action=='release':
                return release(con,row,now,owner)
            if not row or row['state'] not in ('held','sending'):
                return False
            con.execute('UPDATE credit_holds SET state=? WHERE token=?',('spent' if action=='commit' else 'sending',value))
            return True
        if action=='recover':
            # Recover only stale work (>10 min), including during rolling deployments.
            # A live request in the old container must never be refunded by the new one.
            # A send interrupted by a crash is ambiguous: retain debit rather than
            # double-credit on restart; owner may manually compensate after review.
            rows=con.execute("SELECT * FROM credit_holds WHERE state='held' AND created<?",(now-600,)).fetchall()
            for row in rows: release(con,row,now,owner)
            con.execute("UPDATE credit_holds SET state='review' WHERE state='sending' AND created<?",(now-600,))
            return len(rows)
        if action=='ref_stats':
            p=person(con,uid,now)
            rows=con.execute('SELECT COUNT(*),COALESCE(SUM(rewarded),0) FROM credit_people WHERE referrer=?',(uid,)).fetchone()
            reward=con.execute("SELECT COALESCE(SUM(amount),0) FROM credit_events WHERE user_id=? AND kind='referral'",(uid,)).fetchone()[0]
            commissions=con.execute("SELECT COALESCE(SUM(commission),0) FROM shop_charges WHERE referrer=? AND state='granted'",(uid,)).fetchone()[0]
            state=wallet(con,uid,now,owner)
            return dict(code=p['code'],invited=rows[0],qualified=rows[1],pending=rows[0]-rows[1],reward=reward,commission=commissions,
                        referral_balance=state['referral_balance'],referral_total=state['referral_total'],
                        referral_limit=REFERRAL_CAP,referral_reserved=state['referral_reserved'],referral_slots=state['referral_slots'])
        if action=='first_answer':
            person(con,uid,now)
            con.execute('UPDATE credit_people SET first_answer=1 WHERE user_id=?',(uid,))
            return
        if action=='qualify':
            p=person(con,uid,now)
            if not value or not p['first_answer'] or p['rewarded'] or not p['referrer']:
                return None
            # Both IDs remain in anti-abuse records after /delete; never pay twice.
            ref=p['referrer']
            banned=con.execute('SELECT 1 FROM bot_bans WHERE user_id IN (?,?)',(uid,ref)).fetchone()
            if banned:return None
            if not credit(con,ref,'referral',REFERRAL_REWARD,'ref:'+str(uid),now,owner):
                return None
            con.execute('UPDATE credit_people SET rewarded=1 WHERE user_id=?',(uid,))
            return ref
        if action=='gift_prepare':
            if not owner or uid!=owner:return None
            target=int(value)
            if not con.execute('SELECT 1 FROM bot_members WHERE user_id=?',(target,)).fetchone():return None
            token=secrets.token_hex(16)
            con.execute('INSERT INTO credit_admin_gifts VALUES(?,?,?,?,0)',(token,uid,target,now+600))
            return token
        if action=='gift_apply':
            if not owner or uid!=owner:return None
            r=con.execute('SELECT * FROM credit_admin_gifts WHERE token=? AND owner_id=?',(value,uid)).fetchone()
            if not r or r['used'] or now>r['expires']:return None
            if con.execute('SELECT 1 FROM bot_bans WHERE user_id=?',(r['user_id'],)).fetchone():return None
            con.execute('UPDATE credit_admin_gifts SET used=1 WHERE token=?',(value,))
            credit(con,r['user_id'],'admin',1_000_000,'gift:'+value,now,owner)
            return r['user_id']
        if action=='create_order':
            product=value['product']
            if product not in CATALOG:raise ValueError('Unknown product')
            _,_,quantity,price=CATALOG[product]
            con.execute('INSERT INTO shop_orders(payload,user_id,merchant_id,kind,product,quantity,price,created) VALUES(?,?,?,?,?,?,?,?)',
                        (value['payload'],uid,value['merchant_id'],'link',product,quantity,price,now))
            return value['payload']
        if action=='invoice':
            r=con.execute('SELECT * FROM shop_orders WHERE payload=? AND user_id=?',(value['link'],uid)).fetchone()
            if not r or r['kind']!='link' or r['merchant_id']!=value['merchant_id'] or now-r['created']>TTL:return None
            if r['invoice_payload']:
                return None  # New explicit purchase must start in the shop; never duplicate a link.
            payload=value['payload']
            con.execute('INSERT INTO shop_orders(payload,user_id,merchant_id,kind,product,quantity,price,created) VALUES(?,?,?,?,?,?,?,?)',
                        (payload,uid,r['merchant_id'],'invoice',r['product'],r['quantity'],r['price'],now))
            con.execute('UPDATE shop_orders SET invoice_payload=? WHERE payload=?',(payload,r['payload']))
            return dict(con.execute('SELECT * FROM shop_orders WHERE payload=?',(payload,)).fetchone())
        if action=='order':
            r=con.execute('SELECT * FROM shop_orders WHERE payload=? AND user_id=?',(value,uid)).fetchone()
            return dict(r) if r else None
        if action=='precheck':
            payload,amount,currency,merchant=value[:4]
            r=con.execute('SELECT * FROM shop_orders WHERE payload=? AND user_id=?',(payload,uid)).fetchone()
            ok=bool(r and r['kind']=='invoice' and r['merchant_id']==merchant and r['state'] in ('new','checkout')
                    and r['accepted_terms'] and now-r['created']<=TTL and amount==r['price'] and currency=='XTR')
            if ok:con.execute("UPDATE shop_orders SET state='checkout' WHERE payload=?",(payload,))
            return ok
        if action=='payment':
            charge,payload,amount,currency,merchant=value[:5]
            known=con.execute('SELECT * FROM shop_charges WHERE charge_id=?',(charge,)).fetchone()
            if known:return dict(state='duplicate')
            r=con.execute('SELECT * FROM shop_orders WHERE payload=? AND user_id=?',(payload,uid)).fetchone()
            valid=bool(r and r['kind']=='invoice' and r['merchant_id']==merchant and r['state']=='checkout'
                       and r['accepted_terms'] and r['price']==amount and currency=='XTR')
            refunded=con.execute('SELECT 1 FROM shop_refunds WHERE charge_id=? AND user_id=? AND merchant_id=?',(charge,uid,merchant)).fetchone()
            state='refunded' if refunded else 'granted' if valid else 'orphan'
            prod=r['product'] if r else ''
            quantity=r['quantity'] if r else 0
            person(con,uid,now)
            p=con.execute('SELECT referrer FROM credit_people WHERE user_id=?',(uid,)).fetchone()
            ref=p[0] if p else None
            commission=quantity//20 if prod=='tokens' and ref else 0
            con.execute('INSERT INTO shop_charges VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (charge,payload,uid,amount,currency,merchant,prod,quantity,now,state,ref,commission))
            if valid:con.execute("UPDATE shop_orders SET state='paid' WHERE payload=?",(payload,))
            if state=='granted':
                kind=CATALOG[prod][1]
                credit(con,uid,kind,quantity,'pay:'+charge,now,owner)
                if commission:credit(con,ref,'commission',commission,'commission:'+charge,now,owner)
            return dict(state=state,product=prod,quantity=quantity)
        if action=='charge':
            r=con.execute('SELECT * FROM shop_charges WHERE charge_id=?',(value,)).fetchone()
            return dict(r) if r else None
        if action=='refund':
            charge,merchant=value[:2]
            r=con.execute('SELECT * FROM shop_charges WHERE charge_id=?',(charge,)).fetchone()
            if r and (r['user_id']!=uid or r['merchant_id']!=merchant):return False
            con.execute('INSERT OR IGNORE INTO shop_refunds VALUES(?,?,?,?)',(charge,uid,merchant,now))
            if r and r['state']=='granted':
                debit_refund(con,uid,CATALOG[r['product']][1],r['quantity'],now,owner)
                if r['commission']:debit_refund(con,r['referrer'],'tokens',r['commission'],now,owner)
            if r:con.execute("UPDATE shop_charges SET state='refunded' WHERE charge_id=?",(charge,))
            return True
        if action=='payments':
            return [dict(r) for r in con.execute('SELECT * FROM shop_charges ORDER BY paid_at DESC LIMIT 10')]
        if action=='cleanup':
            # Never erase financial/referral records or live reservations.
            con.execute("DELETE FROM shop_orders WHERE state='new' AND created<?",(now-7*86400,))
            con.execute('DELETE FROM credit_admin_gifts WHERE expires<?',(now-86400,))
            return
        raise ValueError('Unknown economy action: '+action)
