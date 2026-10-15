"""Local accounting for one-time 5 XTR purchases of 7 days of Pro.
No Telegram calls here. All grants and quotas are transaction-safe.
"""
import sqlite3
import time
from contextlib import closing

PRICE = 5
DURATION = 7 * 24 * 60 * 60
FREE_LIMIT = 10
WINDOW = 60 * 60
ORDER_TTL = 30 * 60


def connect(path):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init(path):
    with closing(connect(path)) as con, con:
        con.executescript('''
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
        CREATE TABLE IF NOT EXISTS rate_limits (
            user_id INTEGER PRIMARY KEY, last_ts REAL NOT NULL
        );
        ''')
        # Existing v3/v4 databases keep their purchases and access. Merchant 0 means legacy main bot.
        for table, column, declaration in [
            ('pro_orders', 'merchant_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('pro_orders', 'kind', "TEXT NOT NULL DEFAULT 'invoice'"),
            ('pro_charges', 'merchant_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('pro_refunds', 'merchant_id', 'INTEGER NOT NULL DEFAULT 0'),
        ]:
            columns = {row['name'] for row in con.execute(f'PRAGMA table_info({table})')}
            if column not in columns:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')


def merchant_matches(stored, actual, legacy_main):
    return stored == actual or (stored == 0 and actual == legacy_main)


def pro_until(con, uid):
    row = con.execute('SELECT until_ts FROM pro_entitlements WHERE user_id=?', (uid,)).fetchone()
    return row[0] if row else 0.0


def recompute(con, uid):
    expiry = 0.0
    for row in con.execute("SELECT paid_at FROM pro_charges WHERE user_id=? AND state='granted' ORDER BY paid_at,charge_id", (uid,)):
        expiry = max(expiry, row[0]) + DURATION
    con.execute('INSERT INTO pro_entitlements VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET until_ts=excluded.until_ts', (uid, expiry))
    return expiry


def snapshot(con, uid, now):
    until = pro_until(con, uid)
    row = con.execute('SELECT COUNT(*),MIN(at_ts) FROM hour_usage WHERE user_id=? AND at_ts>?',
                      (uid, now - WINDOW)).fetchone()
    return {'pro': until > now, 'until': until, 'used': row[0],
            'remaining': max(0, FREE_LIMIT - row[0]),
            'next_slot': row[1] + WINDOW if row[1] is not None else now}


def run(path, action, uid=0, value=None, now=None):
    now = time.time() if now is None else float(now)
    with closing(connect(path)) as con, con:
        if action in ('reserve', 'payment', 'refund', 'precheck'):
            con.execute('BEGIN IMMEDIATE')
        if action == 'status':
            return snapshot(con, uid, now)
        if action == 'reserve':
            cooldown = float(value.get('cooldown', 0)) if isinstance(value, dict) else float(value or 0)
            exempt = bool(value.get('bypass_quota', False)) if isinstance(value, dict) else False
            state = snapshot(con, uid, now)
            rate = con.execute('SELECT last_ts FROM rate_limits WHERE user_id=?', (uid,)).fetchone()
            if rate and now - rate[0] < cooldown:
                return {'ok': False, 'reason': 'cooldown'}
            if not exempt and not state['pro'] and state['remaining'] == 0:
                return {'ok': False, 'reason': 'quota', 'next_slot': state['next_slot']}
            con.execute('INSERT INTO rate_limits VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_ts=excluded.last_ts', (uid, now))
            reservation = None
            if not exempt and not state['pro']:
                reservation = con.execute('INSERT INTO hour_usage(user_id,at_ts) VALUES(?,?)', (uid, now)).lastrowid
            return {'ok': True, 'reservation': reservation}
        if action == 'release':
            if value is not None:
                con.execute('DELETE FROM hour_usage WHERE id=? AND user_id=?', (value, uid))
            return
        if action == 'create_order':
            item = value if isinstance(value, dict) else {'payload': value}
            con.execute('INSERT INTO pro_orders(payload,user_id,created,merchant_id,kind) VALUES(?,?,?,?,?)',
                        (item['payload'], uid, now, int(item.get('merchant_id', 0)), item.get('kind', 'invoice')))
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
                         and amount == PRICE and currency == 'XTR')
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
                         and order['accepted_terms'] == 1 and amount == PRICE and currency == 'XTR')
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
            con.execute('DELETE FROM hour_usage WHERE at_ts<=?', (now - WINDOW,))
            con.execute('DELETE FROM rate_limits WHERE last_ts<=?', (now - WINDOW,))
            # Unpaid checkout state is not erased: delayed valid payments must remain matchable.
            con.execute("DELETE FROM pro_orders WHERE state='new' AND created<?", (now - 86400,))
            return
        raise ValueError('Unknown billing action')
