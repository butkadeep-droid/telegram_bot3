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
            accepted_terms INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS pro_charges (
            charge_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
            currency TEXT NOT NULL, paid_at REAL NOT NULL,
            state TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pro_charges_user ON pro_charges(user_id, paid_at);
        CREATE TABLE IF NOT EXISTS pro_entitlements (
            user_id INTEGER PRIMARY KEY, until_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pro_refunds (
            charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, seen_at REAL NOT NULL
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
            cooldown = float(value or 0)
            state = snapshot(con, uid, now)
            rate = con.execute('SELECT last_ts FROM rate_limits WHERE user_id=?', (uid,)).fetchone()
            if rate and now - rate[0] < cooldown:
                return {'ok': False, 'reason': 'cooldown'}
            if not state['pro'] and state['remaining'] == 0:
                return {'ok': False, 'reason': 'quota', 'next_slot': state['next_slot']}
            con.execute('INSERT INTO rate_limits VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_ts=excluded.last_ts', (uid, now))
            reservation = None
            if not state['pro']:
                reservation = con.execute('INSERT INTO hour_usage(user_id,at_ts) VALUES(?,?)', (uid, now)).lastrowid
            return {'ok': True, 'reservation': reservation}
        if action == 'release':
            if value is not None:
                con.execute('DELETE FROM hour_usage WHERE id=? AND user_id=?', (value, uid))
            return
        if action == 'create_order':
            con.execute('INSERT INTO pro_orders(payload,user_id,created) VALUES(?,?,?)', (value, uid, now))
            return value
        if action == 'precheck':
            payload, amount, currency = value
            order = con.execute('SELECT * FROM pro_orders WHERE payload=?', (payload,)).fetchone()
            valid = bool(order and order['user_id'] == uid and order['state'] in ('new', 'checkout')
                         and order['accepted_terms'] == 1 and now - order['created'] <= ORDER_TTL
                         and amount == PRICE and currency == 'XTR')
            if valid:
                con.execute("UPDATE pro_orders SET state='checkout' WHERE payload=?", (payload,))
            return valid
        if action == 'payment':
            charge, payload, amount, currency = value
            existing = con.execute('SELECT * FROM pro_charges WHERE charge_id=?', (charge,)).fetchone()
            if existing:
                return {'state': 'duplicate', 'until': pro_until(con, existing['user_id'])}
            order = con.execute('SELECT * FROM pro_orders WHERE payload=?', (payload,)).fetchone()
            valid = bool(order and order['user_id'] == uid and order['state'] == 'checkout'
                         and order['accepted_terms'] == 1 and amount == PRICE and currency == 'XTR')
            refund = con.execute('SELECT user_id FROM pro_refunds WHERE charge_id=?', (charge,)).fetchone()
            state = 'refunded' if refund else ('granted' if valid else 'orphan')
            con.execute('INSERT INTO pro_charges VALUES(?,?,?,?,?,?,?)', (charge, payload, uid, amount, currency, now, state))
            if valid:
                con.execute("UPDATE pro_orders SET state='paid' WHERE payload=?", (payload,))
            until = recompute(con, uid) if state == 'granted' else pro_until(con, uid)
            return {'state': state, 'until': until}
        if action == 'refund':
            charge = value
            row = con.execute('SELECT user_id FROM pro_charges WHERE charge_id=?', (charge,)).fetchone()
            if row and row['user_id'] != uid:
                raise ValueError('Refund user mismatch')
            con.execute('INSERT OR IGNORE INTO pro_refunds VALUES(?,?,?)', (charge, uid, now))
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

