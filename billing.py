"""Local accounting for one-time Telegram Stars purchases of 7 days of Plus.
No Telegram calls here. All grants and quotas are transaction-safe.
"""
import sqlite3
import secrets
import time
from contextlib import closing

PRICE = 5  # Regular price restored. Already confirmed orders keep their recorded price.
DURATION = 7 * 24 * 60 * 60
FREE_LIMIT = 20
SMART_LIMIT = 5
GOSHA_BONUS = 0  # Easter egg retired; legacy API cannot grant it again.
GIFT_DURATION = 24 * 60 * 60
GIFT_TTL = 10 * 60
WINDOW = 60 * 60
ORDER_TTL = 30 * 60


def connect(path):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init(path):
    with closing(connect(path)) as con, con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS friend_roles (
            user_id INTEGER PRIMARY KEY, granted_by INTEGER NOT NULL, granted_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pro_orders (
            payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'new',
            accepted_terms INTEGER NOT NULL DEFAULT 1,
            merchant_id INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'invoice'
        );
        CREATE TABLE IF NOT EXISTS pro_charges (
            charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
            currency TEXT NOT NULL, paid_at REAL NOT NULL,
            state TEXT NOT NULL, merchant_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS pro_charges_user ON pro_charges(user_id, paid_at);
        CREATE TABLE IF NOT EXISTS pro_entitlements (
            user_id INTEGER PRIMARY KEY, until_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pro_refunds (
            charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, seen_at REAL NOT NULL,
            merchant_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS hour_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, at_ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS hour_usage_user ON hour_usage(user_id, at_ts);
        CREATE TABLE IF NOT EXISTS smart_hour_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, at_ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS smart_hour_usage_user ON smart_hour_usage(user_id, at_ts);
        CREATE TABLE IF NOT EXISTS rate_limits (
            user_id INTEGER PRIMARY KEY, last_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pro_gifts (
            gift_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
            created REAL NOT NULL, duration_seconds INTEGER NOT NULL DEFAULT 86400,
            granted_at REAL
        );
        CREATE INDEX IF NOT EXISTS pro_gifts_user ON pro_gifts(user_id, granted_at);
        CREATE TABLE IF NOT EXISTS pro_revoke_requests (
            revoke_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,
            created REAL NOT NULL,expected_until REAL NOT NULL,executed_at REAL
        );
        CREATE TABLE IF NOT EXISTS bonus_retirement_notices (
            user_id INTEGER PRIMARY KEY, created REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', next_attempt REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT
        );
        CREATE TABLE IF NOT EXISTS permanent_bonuses (
            user_id INTEGER PRIMARY KEY, gosha_claimed_at REAL NOT NULL
        );
        ''')
        con.execute('BEGIN IMMEDIATE')
        # Existing v3/v4 databases keep their purchases and access. Merchant 0 means legacy main bot.
        for table, column, declaration in [
            ('pro_orders', 'merchant_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('pro_orders', 'kind', "TEXT NOT NULL DEFAULT 'invoice'"),
            ('pro_orders', 'price', 'INTEGER NOT NULL DEFAULT 5'),
            ('pro_charges', 'merchant_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('pro_refunds', 'merchant_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('pro_charges', 'revoked_at', 'REAL'),
            ('pro_gifts', 'revoked_at', 'REAL'),
        ]:
            columns = {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}
            if column not in columns:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')

        # Atomically remember only prior recipients, then revoke their old bonus.
        # INSERT OR IGNORE preserves delivery state over redeploys and restarts.
        con.execute('INSERT OR IGNORE INTO bonus_retirement_notices(user_id,created) '
                    'SELECT user_id,? FROM permanent_bonuses WHERE user_id>0', (time.time(),))
        con.execute('DELETE FROM permanent_bonuses')


def merchant_matches(stored, actual, legacy_main):
    return stored == actual or (stored == 0 and actual == legacy_main)


def pro_until(con, uid):
    row = con.execute('SELECT until_ts FROM pro_entitlements WHERE user_id=?', (uid,)).fetchone()
    return row[0] if row else 0.0


def recompute(con, uid):
    expiry = 0.0
    events = [(row[0], 0, row[1], DURATION) for row in con.execute(
        "SELECT paid_at,charge_id FROM pro_charges WHERE user_id=? AND state='granted' AND revoked_at IS NULL", (uid,))]
    events += [(row[0], 1, row[1], row[2]) for row in con.execute(
        "SELECT granted_at,gift_id,duration_seconds FROM pro_gifts WHERE user_id=? AND granted_at IS NOT NULL AND revoked_at IS NULL", (uid,))]
    for when, _, event_id, duration in sorted(events):
        expiry = max(expiry, when) + duration
    con.execute('INSERT INTO pro_entitlements VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET until_ts=excluded.until_ts', (uid, expiry))
    return expiry


def snapshot(con, uid, now):
    until = pro_until(con, uid)
    bonus = 0  # Retired bonuses cannot affect quota, even if an old row reappears.
    limit = FREE_LIMIT + bonus
    row = con.execute('SELECT COUNT(*),MIN(at_ts) FROM hour_usage WHERE user_id=? AND at_ts>?',
                      (uid, now - WINDOW)).fetchone()
    smart = con.execute('SELECT COUNT(*),MIN(at_ts) FROM smart_hour_usage WHERE user_id=? AND at_ts>?',
                        (uid, now - WINDOW)).fetchone()
    friend = con.execute('SELECT 1 FROM friend_roles WHERE user_id=?', (uid,)).fetchone() is not None
    return {'friend': friend, 'smart_used': smart[0], 'smart_limit': SMART_LIMIT,
            'smart_remaining': max(0, SMART_LIMIT - smart[0]),
            'smart_next_slot': smart[1] + WINDOW if smart[1] is not None else now,
            'pro': until > now, 'until': until, 'used': row[0],
            'remaining': max(0, limit - row[0]), 'limit': limit, 'bonus': bonus,
            'next_slot': row[1] + WINDOW if row[1] is not None else now}


def run(path, action, uid=0, value=None, now=None):
    now = time.time() if now is None else float(now)
    with closing(connect(path)) as con, con:
        if action in ('reserve', 'payment', 'refund', 'precheck', 'gosha_bonus', 'gift_grant', 'bonus_notice_claim', 'bonus_notice_finish', 'revoke_prepare', 'revoke_apply'):
            con.execute('BEGIN IMMEDIATE')
        if action == 'revoke_prepare':
            recipient=int(value['recipient_id'])
            if uid<=0 or recipient<=0 or uid==recipient:
                return {'state':'invalid'}
            until=pro_until(con,recipient)
            if until<=now:return {'state':'inactive'}
            con.execute('INSERT INTO pro_revoke_requests VALUES(?,?,?,?,?,NULL)',
                        (value['token'],recipient,uid,now,until))
            return {'state':'prepared','until':until}
        if action == 'revoke_apply':
            request=con.execute('SELECT * FROM pro_revoke_requests WHERE revoke_id=? AND owner_id=?',(value,uid)).fetchone()
            if not request:return {'state':'invalid'}
            recipient=request['user_id']
            if request['executed_at'] is not None:return {'state':'duplicate','user_id':recipient}
            if now-request['created']>GIFT_TTL:return {'state':'expired'}
            until=pro_until(con,recipient)
            if until!=request['expected_until']:return {'state':'changed'}
            if until<=now:return {'state':'inactive'}
            # Preserve payment/refund accounting. Exclude only prior grants from all future recomputations.
            con.execute("UPDATE pro_charges SET revoked_at=? WHERE user_id=? AND state='granted' AND revoked_at IS NULL",(now,recipient))
            con.execute('UPDATE pro_gifts SET revoked_at=? WHERE user_id=? AND granted_at IS NOT NULL AND revoked_at IS NULL',(now,recipient))
            con.execute('DELETE FROM pro_gifts WHERE user_id=? AND granted_at IS NULL',(recipient,))
            con.execute('UPDATE pro_revoke_requests SET executed_at=? WHERE revoke_id=?',(now,value))
            recompute(con,recipient)
            return {'state':'revoked','user_id':recipient,'until':until}
        if action == 'gift_prepare':
            recipient = int(value['recipient_id'])
            if uid <= 0 or recipient <= 0:
                raise ValueError('Gift owner and recipient must be positive IDs')
            con.execute('INSERT INTO pro_gifts(gift_id,user_id,owner_id,created,duration_seconds) VALUES(?,?,?,?,?)',
                        (value['token'], recipient, uid, now, GIFT_DURATION))
            return value['token']
        if action == 'gift_grant':
            gift = con.execute('SELECT * FROM pro_gifts WHERE gift_id=? AND owner_id=?', (value, uid)).fetchone()
            if not gift:
                return {'state': 'invalid'}
            if gift['granted_at'] is not None:
                return {'state': 'duplicate', 'user_id': gift['user_id'], 'until': pro_until(con, gift['user_id'])}
            if now - gift['created'] > GIFT_TTL:
                return {'state': 'expired'}
            con.execute('UPDATE pro_gifts SET granted_at=? WHERE gift_id=?', (now, value))
            until = recompute(con, gift['user_id'])
            return {'state': 'granted', 'user_id': gift['user_id'], 'until': until}
        if action == 'status':
            return snapshot(con, uid, now)
        if action == 'gosha_bonus':
            return {'granted': False, 'retired': True, **snapshot(con, uid, now)}
        if action == 'bonus_notice_claim':
            row = con.execute("SELECT user_id FROM bonus_retirement_notices "
                              "WHERE state IN ('pending','sending') AND next_attempt<=? "
                              "ORDER BY created,user_id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            con.execute("UPDATE bonus_retirement_notices SET state='sending',next_attempt=?, "
                        "attempts=attempts+1,claim_token=? WHERE user_id=?", (now+300,token,row['user_id']))
            return {'user_id': row['user_id'], 'token': token}
        if action == 'bonus_notice_finish':
            state = value['state']
            if state not in ('sent','blocked','pending'):
                raise ValueError('Invalid notification state')
            delay = max(1, float(value.get('delay',300)))
            con.execute("UPDATE bonus_retirement_notices SET state=?,next_attempt=? "
                        "WHERE user_id=? AND state='sending' AND claim_token=?",
                        (state, now+delay,uid,value['token']))
            return
        if action == 'reserve':
            cooldown = float(value.get('cooldown', 0)) if isinstance(value, dict) else float(value or 0)
            exempt = bool(value.get('bypass_quota', False)) if isinstance(value, dict) else False
            smart_mode = isinstance(value, dict) and bool(value.get('smart') or value.get('require_pro'))
            state = snapshot(con, uid, now)
            if state['friend'] and not exempt:
                cooldown = 5
                exempt = True
            # Authorize smart generation inside the same transaction as its reservation.
            if isinstance(value, dict) and value.get('require_pro') and not exempt and not state['pro']:
                return {'ok': False, 'reason': 'pro_required'}
            if smart_mode and not exempt and state['smart_remaining'] == 0:
                return {'ok': False, 'reason': 'smart_quota', 'limit': SMART_LIMIT,
                        'used': state['smart_used'], 'next_slot': state['smart_next_slot']}
            rate = con.execute('SELECT last_ts FROM rate_limits WHERE user_id=?', (uid,)).fetchone()
            if rate and now - rate[0] < cooldown:
                return {'ok': False, 'reason': 'cooldown', 'cooldown': cooldown}
            if not exempt and not state['pro'] and state['remaining'] == 0:
                return {'ok': False, 'reason': 'quota', 'next_slot': state['next_slot'], 'limit': state['limit'], 'used': state['used']}
            con.execute('INSERT INTO rate_limits VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_ts=excluded.last_ts', (uid, now))
            reservation = None
            if smart_mode and not exempt:
                row_id = con.execute('INSERT INTO smart_hour_usage(user_id,at_ts) VALUES(?,?)', (uid, now)).lastrowid
                reservation = {'scope': 'smart', 'id': row_id}
            elif not exempt and not state['pro']:
                reservation = con.execute('INSERT INTO hour_usage(user_id,at_ts) VALUES(?,?)', (uid, now)).lastrowid
            return {'ok': True, 'reservation': reservation}
        if action == 'release':
            if isinstance(value, dict) and value.get('scope') == 'smart':
                con.execute('DELETE FROM smart_hour_usage WHERE id=? AND user_id=?', (value['id'], uid))
            elif value is not None:
                con.execute('DELETE FROM hour_usage WHERE id=? AND user_id=?', (value, uid))
            return
        if action == 'create_order':
            item = value if isinstance(value, dict) else {'payload': value}
            con.execute('INSERT INTO pro_orders(payload,user_id,created,merchant_id,kind,price) VALUES(?,?,?,?,?,?)',
                        (item['payload'], uid, now, int(item.get('merchant_id', 0)), item.get('kind', 'invoice'), PRICE))
            return item['payload']
        if action == 'order':
            row = con.execute('SELECT * FROM pro_orders WHERE payload=? AND user_id=?', (value, uid)).fetchone()
            return dict(row) if row else None
        if action == 'precheck':
            payload, amount, currency = value[:3]
            merchant = int(value[3]) if len(value) > 3 else 0
            legacy_main = int(value[4]) if len(value) > 4 else 0
            order = con.execute('SELECT * FROM pro_orders WHERE payload=?', (payload,)).fetchone()
            valid = bool(order and order['user_id'] == uid and order['state'] in ('new', 'checkout')
                         and order['kind'] == 'invoice' and merchant_matches(order['merchant_id'], merchant, legacy_main)
                         and order['accepted_terms'] == 1 and now - order['created'] <= ORDER_TTL
                         and amount == order['price'] and order['price'] == PRICE and currency == 'XTR')
            if valid:
                con.execute("UPDATE pro_orders SET state='checkout' WHERE payload=?", (payload,))
            return valid
        if action == 'payment':
            charge, payload, amount, currency = value[:4]
            merchant = int(value[4]) if len(value) > 4 else 0
            legacy_main = int(value[5]) if len(value) > 5 else 0
            existing = con.execute('SELECT * FROM pro_charges WHERE charge_id=?', (charge,)).fetchone()
            if existing:
                return {'state': 'duplicate', 'until': pro_until(con, existing['user_id'])}
            order = con.execute('SELECT * FROM pro_orders WHERE payload=?', (payload,)).fetchone()
            valid = bool(order and order['user_id'] == uid and order['state'] == 'checkout'
                         and order['kind'] == 'invoice' and merchant_matches(order['merchant_id'], merchant, legacy_main)
                         and order['accepted_terms'] == 1 and amount == order['price'] and currency == 'XTR')
            refund = con.execute('SELECT user_id,merchant_id FROM pro_refunds WHERE charge_id=?', (charge,)).fetchone()
            refund = refund if refund and merchant_matches(refund['merchant_id'], merchant, legacy_main) else None
            state = 'refunded' if refund else ('granted' if valid else 'orphan')
            con.execute('INSERT INTO pro_charges(charge_id,payload,user_id,amount,currency,paid_at,state,merchant_id) VALUES(?,?,?,?,?,?,?,?)', (charge, payload, uid, amount, currency, now, state, merchant))
            if valid:
                con.execute("UPDATE pro_orders SET state='paid' WHERE payload=?", (payload,))
            until = recompute(con, uid) if state == 'granted' else pro_until(con, uid)
            return {'state': state, 'until': until}
        if action == 'refund':
            charge = value[0] if isinstance(value, (tuple, list)) else value
            merchant = int(value[1]) if isinstance(value, (tuple, list)) and len(value) > 1 else 0
            legacy_main = int(value[2]) if isinstance(value, (tuple, list)) and len(value) > 2 else 0
            row = con.execute('SELECT user_id,merchant_id FROM pro_charges WHERE charge_id=?', (charge,)).fetchone()
            if row and row['user_id'] != uid:
                raise ValueError('Refund user mismatch')
            if row and not merchant_matches(row['merchant_id'], merchant, legacy_main):
                raise ValueError('Refund merchant mismatch')
            con.execute('INSERT OR IGNORE INTO pro_refunds(charge_id,user_id,seen_at,merchant_id) VALUES(?,?,?,?)', (charge, uid, now, merchant))
            con.execute("UPDATE pro_charges SET state='refunded' WHERE charge_id=? AND user_id=?", (charge, uid))
            return recompute(con, uid)
        if action == 'charge':
            row = con.execute('SELECT * FROM pro_charges WHERE charge_id=?', (value,)).fetchone()
            return dict(row) if row else None
        if action == 'payments':
            return [dict(row) for row in con.execute('SELECT * FROM pro_charges ORDER BY paid_at DESC LIMIT 10')]
        if action == 'cleanup':
            con.execute('DELETE FROM pro_revoke_requests WHERE executed_at IS NULL AND created<?',(now-86400,))
            con.execute('DELETE FROM hour_usage WHERE at_ts<=?', (now - WINDOW,))
            con.execute('DELETE FROM smart_hour_usage WHERE at_ts<=?', (now - WINDOW,))
            con.execute("DELETE FROM bonus_retirement_notices WHERE state IN ('sent','blocked') AND created<?", (now-90*86400,))
            con.execute('DELETE FROM rate_limits WHERE last_ts<=?', (now - WINDOW,))
            con.execute('DELETE FROM pro_gifts WHERE granted_at IS NULL AND created<?', (now - 86400,))
            # Unpaid checkout state is not erased: delayed valid payments must remain matchable.
            con.execute("DELETE FROM pro_orders WHERE state='new' AND created<?", (now - 86400,))
            return
        raise ValueError('Unknown billing action')
