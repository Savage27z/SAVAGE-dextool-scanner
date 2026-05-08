import json
import os
import re
from datetime import date

import asyncpg

try:
    import redis.asyncio as redis
except Exception:
    redis = None

from config import logger

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://savage:savage@localhost:5432/savage_trading')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
_pool: asyncpg.Pool | None = None
_redis_client = None

_LOGIN_CODE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS dashboard_login_codes (
    code VARCHAR(10) PRIMARY KEY,
    user_id BIGINT,
    username VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    claimed_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ
)
"""


def _convert_sql(sql: str) -> str:
    sql = sql.replace('INSERT OR IGNORE', 'INSERT')
    sql = sql.replace('INSERT OR REPLACE', 'INSERT')
    sql = sql.replace('CURRENT_TIMESTAMP', 'NOW()')
    sql = sql.replace("DATE('now')", 'CURRENT_DATE')
    sql = sql.replace("date('now')", 'CURRENT_DATE')
    sql = re.sub(r"DATE\(closed_at\)", 'DATE(closed_at)', sql)
    sql = re.sub(r"datetime\('now', \?\)", '(NOW() + ($1)::interval)', sql)
    out = []
    idx = 1
    in_str = False
    quote = ''
    for ch in sql:
        if ch in "'\"":
            if in_str and ch == quote:
                in_str = False
            elif not in_str:
                in_str = True
                quote = ch
        if ch == '?' and not in_str:
            out.append(f'${idx}')
            idx += 1
        else:
            out.append(ch)
    return ''.join(out)


def _row(row):
    return dict(row) if row else None


async def _ensure_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        await init_db()
    return _pool


async def init_db():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=20)
    try:
        await _pool.execute(_LOGIN_CODE_TABLE_DDL)
    except Exception as exc:
        logger.warning('Failed to ensure dashboard_login_codes table at startup: %s', exc)
    logger.info('PostgreSQL pool initialised')


async def close_db():
    global _pool, _redis_client
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def _publish_event(event_type: str, data: dict, user_id: int | None = None):
    global _redis_client
    if redis is None:
        return
    try:
        if _redis_client is None:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        payload = {'type': event_type, 'data': data}
        if user_id is not None:
            payload['user_id'] = user_id
        await _redis_client.publish('trading_events', json.dumps(payload, default=str))
    except Exception as exc:
        logger.debug('Redis publish failed: %s', exc)


async def _fetch(sql: str, *args):
    pool = await _ensure_pool()
    return [dict(r) for r in await pool.fetch(_convert_sql(sql), *args)]


async def _fetchrow(sql: str, *args):
    pool = await _ensure_pool()
    return _row(await pool.fetchrow(_convert_sql(sql), *args))


async def _fetchval(sql: str, *args):
    pool = await _ensure_pool()
    return await pool.fetchval(_convert_sql(sql), *args)


async def _execute(sql: str, *args) -> str:
    pool = await _ensure_pool()
    return await pool.execute(_convert_sql(sql), *args)


async def _executemany(sql: str, rows):
    pool = await _ensure_pool()
    await pool.executemany(_convert_sql(sql), rows)


def _affected(status: str) -> int:
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


async def save_detected_token(token_data: dict):
    social = token_data.get('social_links')
    if isinstance(social, dict):
        social = json.dumps(social)
    await _execute('''
        INSERT INTO detected_tokens
            (name, symbol, contract_address, chain, market_cap, liquidity, price_usd, price_native, volume_24h, price_change_24h, holders, buy_tax, sell_tax, dextools_url, dex_pair_url, deployer_wallet, social_links)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (contract_address, chain) DO NOTHING
    ''', token_data.get('name'), token_data.get('symbol'), token_data.get('contract_address'), token_data.get('chain'), token_data.get('market_cap'), token_data.get('liquidity'), token_data.get('price_usd'), token_data.get('price_native'), token_data.get('volume_24h'), token_data.get('price_change_24h'), token_data.get('holders'), token_data.get('buy_tax'), token_data.get('sell_tax'), token_data.get('dextools_url'), token_data.get('dex_pair_url'), token_data.get('deployer_wallet'), social)


async def save_open_position(position: dict):
    await _execute('''
        INSERT INTO open_positions (token_address, token_symbol, chain, entry_price, tokens_received, buy_amount_native, buy_tx_hash, pair_address, entry_liquidity, user_id, tiers_completed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (token_address, chain, user_id) DO NOTHING
    ''', position['token_address'], position['token_symbol'], position['chain'], position['entry_price'], position['tokens_received'], position['buy_amount_native'], position['buy_tx_hash'], position.get('pair_address'), position.get('entry_liquidity', 0), position.get('user_id', 0), '[]')
    await _publish_event('position_update', position, position.get('user_id', 0))
    logger.info('Saved open position for %s on %s (user %d)', position['token_symbol'], position['chain'], position.get('user_id', 0))


async def get_open_positions(user_id: int | None = None) -> list[dict]:
    if user_id is not None:
        return await _fetch('SELECT * FROM open_positions WHERE user_id = ? ORDER BY opened_at DESC', user_id)
    return await _fetch('SELECT * FROM open_positions ORDER BY opened_at DESC')


async def close_position(token_address: str, chain: str, exit_data: dict, user_id: int = 0):
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            pos = await conn.fetchrow('SELECT * FROM open_positions WHERE token_address = $1 AND chain = $2 AND user_id = $3', token_address, chain, user_id)
            if pos is None:
                logger.warning('close_position: no open position for %s on %s user %d', token_address, chain, user_id)
                return
            await conn.execute('''
                INSERT INTO completed_trades (token_address, token_symbol, chain, entry_price, exit_price, tokens_amount, buy_amount_native, sell_amount_native, profit_usd, roi_percent, buy_tx_hash, sell_tx_hash, opened_at, duration_seconds, user_id, close_reason)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ''', pos['token_address'], pos['token_symbol'], pos['chain'], pos['entry_price'], exit_data['exit_price'], pos['tokens_received'], pos['buy_amount_native'], exit_data['sell_amount_native'], exit_data.get('profit_usd'), exit_data['roi_percent'], pos['buy_tx_hash'], exit_data['sell_tx_hash'], pos['opened_at'], exit_data.get('duration_seconds', 0), user_id, exit_data.get('close_reason'))
            await conn.execute('DELETE FROM open_positions WHERE token_address = $1 AND chain = $2 AND user_id = $3', token_address, chain, user_id)
    await _publish_event('trade_closed', {'token_address': token_address, 'chain': chain, 'roi_percent': exit_data.get('roi_percent'), 'close_reason': exit_data.get('close_reason')}, user_id)


async def update_tiers_completed(token_address: str, chain: str, tiers_completed: list[int], user_id: int = 0):
    await _execute('UPDATE open_positions SET tiers_completed = ? WHERE token_address = ? AND chain = ? AND user_id = ?', json.dumps(tiers_completed), token_address, chain, user_id)


async def mark_tp1_hit(token_address: str, chain: str, user_id: int = 0):
    await _execute('UPDATE open_positions SET tp1_hit = TRUE WHERE token_address = ? AND chain = ? AND user_id = ?', token_address, chain, user_id)


async def record_partial_sell(token_address: str, chain: str, user_id: int, sell_fraction: float, exit_data: dict):
    pool = await _ensure_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            pos = await conn.fetchrow('SELECT * FROM open_positions WHERE token_address = $1 AND chain = $2 AND user_id = $3', token_address, chain, user_id)
            if pos is None:
                return
            sold_tokens = pos['tokens_received'] * sell_fraction
            sold_buy_native = pos['buy_amount_native'] * sell_fraction
            await conn.execute('''
                INSERT INTO completed_trades (token_address, token_symbol, chain, entry_price, exit_price, tokens_amount, buy_amount_native, sell_amount_native, profit_usd, roi_percent, buy_tx_hash, sell_tx_hash, opened_at, duration_seconds, user_id, close_reason)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ''', pos['token_address'], pos['token_symbol'], pos['chain'], pos['entry_price'], exit_data['exit_price'], sold_tokens, sold_buy_native, exit_data['sell_amount_native'], exit_data.get('profit_usd'), exit_data['roi_percent'], pos['buy_tx_hash'], exit_data['sell_tx_hash'], pos['opened_at'], exit_data.get('duration_seconds', 0), user_id, exit_data.get('close_reason'))
            await conn.execute('UPDATE open_positions SET tokens_received = tokens_received - $1, buy_amount_native = buy_amount_native - $2 WHERE token_address = $3 AND chain = $4 AND user_id = $5', sold_tokens, sold_buy_native, token_address, chain, user_id)
    await _publish_event('trade_closed', {'token_address': token_address, 'chain': chain, 'roi_percent': exit_data.get('roi_percent'), 'close_reason': exit_data.get('close_reason'), 'partial': True}, user_id)


async def get_trade_history(limit: int = 10, user_id: int | None = None) -> list[dict]:
    if user_id is not None:
        return await _fetch('SELECT * FROM completed_trades WHERE user_id = ? ORDER BY closed_at DESC LIMIT ?', user_id, limit)
    return await _fetch('SELECT * FROM completed_trades ORDER BY closed_at DESC LIMIT ?', limit)


async def add_allowed_user(user_id: int, username: str = ''):
    await _execute('INSERT INTO allowed_users (user_id, username) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username', user_id, username)


async def remove_allowed_user(user_id: int) -> bool:
    return _affected(await _execute('DELETE FROM allowed_users WHERE user_id = ?', user_id)) > 0


async def get_allowed_users() -> list[dict]:
    return await _fetch('SELECT * FROM allowed_users ORDER BY added_at')


async def is_user_allowed(user_id: int) -> bool:
    return await _fetchval('SELECT 1 FROM allowed_users WHERE user_id = ? LIMIT 1', user_id) is not None


async def update_peak_price(token_address: str, chain: str, peak_price: float, trailing_activated: bool, user_id: int = 0):
    await _execute('UPDATE open_positions SET peak_price = ?, trailing_activated = ? WHERE token_address = ? AND chain = ? AND user_id = ?', peak_price, int(trailing_activated), token_address, chain, user_id)


async def add_whale_wallet(address: str, label: str = '') -> bool:
    try:
        return _affected(await _execute('INSERT INTO whale_wallets (address, label) VALUES (?, ?) ON CONFLICT DO NOTHING', address, label)) > 0
    except Exception:
        return False


async def remove_whale_wallet(address: str) -> bool:
    return _affected(await _execute('DELETE FROM whale_wallets WHERE address = ?', address)) > 0


async def get_whale_wallets() -> list[dict]:
    return await _fetch('SELECT * FROM whale_wallets ORDER BY added_at')


async def save_whale_event(event: dict) -> bool:
    return _affected(await _execute('INSERT INTO whale_events (wallet_address, token_mint, token_symbol, sol_spent, tokens_received, tx_signature) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (tx_signature) DO NOTHING', event['wallet_address'], event['token_mint'], event.get('token_symbol', ''), event['sol_spent'], event['tokens_received'], event['tx_signature'])) > 0


async def get_whale_events(limit: int = 10) -> list[dict]:
    return await _fetch('SELECT * FROM whale_events ORDER BY detected_at DESC LIMIT ?', limit)


async def is_token_watched(token_mint: str, chain: str = 'SOL') -> dict | None:
    row = await _fetchrow('SELECT * FROM open_positions WHERE token_address = ? AND chain = ? LIMIT 1', token_mint, chain)
    if row:
        return {'source': 'open_positions', **row}
    row = await _fetchrow('SELECT * FROM detected_tokens WHERE contract_address = ? AND chain = ? LIMIT 1', token_mint, chain)
    return {'source': 'detected_tokens', **row} if row else None


async def is_token_already_bought(contract_address: str, chain: str, user_id: int = 0) -> bool:
    return await _fetchval('SELECT 1 FROM open_positions WHERE token_address = ? AND chain = ? AND user_id = ? LIMIT 1', contract_address, chain, user_id) is not None or await _fetchval('SELECT 1 FROM completed_trades WHERE token_address = ? AND chain = ? AND user_id = ? LIMIT 1', contract_address, chain, user_id) is not None


async def save_user_wallet(user_id: int, public_key: str, encrypted_private_key: str, encrypted_seed_phrase: str = ''):
    await _execute('INSERT INTO user_wallets (user_id, public_key, encrypted_private_key, encrypted_seed_phrase) VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET public_key = EXCLUDED.public_key, encrypted_private_key = EXCLUDED.encrypted_private_key, encrypted_seed_phrase = EXCLUDED.encrypted_seed_phrase', user_id, public_key, encrypted_private_key, encrypted_seed_phrase)


async def get_user_wallet(user_id: int) -> dict | None:
    return await _fetchrow('SELECT * FROM user_wallets WHERE user_id = ?', user_id)


async def get_all_trading_users() -> list[dict]:
    return await _fetch('SELECT * FROM user_wallets WHERE auto_trade = 1')


async def set_auto_trade(user_id: int, enabled: bool):
    await _execute('UPDATE user_wallets SET auto_trade = ? WHERE user_id = ?', 1 if enabled else 0, user_id)


async def delete_user_wallet(user_id: int) -> bool:
    return _affected(await _execute('DELETE FROM user_wallets WHERE user_id = ?', user_id)) > 0


async def migrate_legacy_positions(admin_user_id: int):
    await _execute('UPDATE open_positions SET user_id = ? WHERE user_id = 0', admin_user_id)
    await _execute('UPDATE completed_trades SET user_id = ? WHERE user_id = 0', admin_user_id)


async def upsert_bot_chat(chat_id: int, chat_type: str = 'private', title: str = ''):
    await _execute('INSERT INTO bot_chats (chat_id, chat_type, title) VALUES (?, ?, ?) ON CONFLICT (chat_id) DO UPDATE SET chat_type = EXCLUDED.chat_type, title = EXCLUDED.title', chat_id, chat_type, title)


async def remove_bot_chat(chat_id: int):
    await _execute('DELETE FROM bot_chats WHERE chat_id = ?', chat_id)


async def get_all_bot_chats() -> list[dict]:
    return await _fetch('SELECT * FROM bot_chats ORDER BY added_at')


async def record_fee(user_id: int, token_symbol: str, trade_profit: float, fee_amount: float, fee_pct: float, tx_hash: str = '', status: str = 'pending') -> int:
    return await _fetchval('INSERT INTO fee_ledger (user_id, token_symbol, trade_profit_native, fee_amount_native, fee_percent, fee_tx_hash, status) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id', user_id, token_symbol, trade_profit, fee_amount, fee_pct, tx_hash, status)


async def update_fee_status(fee_id: int, status: str, tx_hash: str = ''):
    if tx_hash:
        await _execute('UPDATE fee_ledger SET status = ?, fee_tx_hash = ? WHERE id = ?', status, tx_hash, fee_id)
    else:
        await _execute('UPDATE fee_ledger SET status = ? WHERE id = ?', status, fee_id)


async def get_fee_stats() -> dict:
    row = await _fetchrow("SELECT COALESCE(SUM(CASE WHEN status='collected' THEN fee_amount_native ELSE 0 END), 0) AS total_collected, COALESCE(SUM(CASE WHEN status='pending' OR status='submitted' THEN fee_amount_native ELSE 0 END), 0) AS total_pending, COALESCE(SUM(CASE WHEN status='failed' THEN fee_amount_native ELSE 0 END), 0) AS total_failed, COUNT(*) AS count FROM fee_ledger")
    return row or {'total_collected': 0, 'total_pending': 0, 'total_failed': 0, 'count': 0}


async def count_open_positions(user_id: int) -> int:
    return await _fetchval('SELECT COUNT(*) FROM open_positions WHERE user_id = ?', user_id) or 0


async def get_daily_realized_loss(user_id: int) -> float:
    return float(await _fetchval("SELECT COALESCE(SUM(buy_amount_native - sell_amount_native), 0) FROM completed_trades WHERE user_id = ? AND DATE(closed_at) = CURRENT_DATE AND sell_amount_native < buy_amount_native", user_id) or 0)


async def get_fee_history(limit: int = 20) -> list[dict]:
    return await _fetch('SELECT * FROM fee_ledger ORDER BY created_at DESC LIMIT ?', limit)


async def get_trade_stats(user_id: int | None = None, days: int | None = None) -> dict:
    params = []
    conditions = []
    if user_id is not None:
        conditions.append(f'user_id = ${len(params)+1}')
        params.append(user_id)
    if days is not None:
        conditions.append(f"closed_at >= NOW() - (${len(params)+1} || ' days')::interval")
        params.append(days)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ''
    pool = await _ensure_pool()
    row = await pool.fetchrow(f"""
        SELECT COUNT(*) as total_trades,
               COALESCE(SUM(CASE WHEN roi_percent > 0 THEN 1 ELSE 0 END), 0) as winning_trades,
               COALESCE(SUM(CASE WHEN roi_percent <= 0 THEN 1 ELSE 0 END), 0) as losing_trades,
               COALESCE(AVG(roi_percent), 0) as avg_roi,
               COALESCE(MAX(roi_percent), 0) as best_roi,
               COALESCE(MIN(roi_percent), 0) as worst_roi,
               COALESCE(SUM(sell_amount_native - buy_amount_native), 0) as total_pnl_native,
               COALESCE(SUM(CASE WHEN roi_percent > 0 THEN sell_amount_native - buy_amount_native ELSE 0 END), 0) as total_profit,
               COALESCE(SUM(CASE WHEN roi_percent <= 0 THEN sell_amount_native - buy_amount_native ELSE 0 END), 0) as total_loss,
               COALESCE(SUM(buy_amount_native), 0) as total_invested,
               COALESCE(AVG(duration_seconds), 0) as avg_duration_seconds
        FROM completed_trades {where}
    """, *params)
    stats = dict(row) if row else {}
    best = await pool.fetchrow(f'SELECT token_symbol, roi_percent, buy_amount_native, sell_amount_native FROM completed_trades {where} ORDER BY roi_percent DESC LIMIT 1', *params)
    worst = await pool.fetchrow(f'SELECT token_symbol, roi_percent, buy_amount_native, sell_amount_native FROM completed_trades {where} ORDER BY roi_percent ASC LIMIT 1', *params)
    stats['best_trade'] = dict(best) if best else None
    stats['worst_trade'] = dict(worst) if worst else None
    total = stats.get('total_trades', 0)
    stats['win_rate'] = (stats.get('winning_trades', 0) / total * 100) if total else 0
    loss = stats.get('total_loss', 0)
    profit = stats.get('total_profit', 0)
    stats['profit_factor'] = abs(profit / loss) if loss else (float('inf') if profit > 0 else 0)
    stats['daily_pnl'] = await _fetch("SELECT DATE(closed_at) as day, COUNT(*) as trades, COALESCE(SUM(sell_amount_native - buy_amount_native), 0) as pnl, COALESCE(SUM(CASE WHEN roi_percent > 0 THEN 1 ELSE 0 END), 0) as wins FROM completed_trades WHERE closed_at >= NOW() - interval '7 days' GROUP BY DATE(closed_at) ORDER BY day DESC")
    return stats


async def save_scan_history(token: dict, was_bought: bool = False):
    breakdown = token.get('score_breakdown')
    if isinstance(breakdown, dict):
        breakdown = json.dumps(breakdown)
    await _execute('INSERT INTO scan_history (contract_address, chain, symbol, name, score, score_breakdown, market_cap, liquidity, volume_24h, price_usd, holders, buy_tax, sell_tax, source, was_bought) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', token.get('contract_address', ''), token.get('chain', ''), token.get('symbol', ''), token.get('name', ''), token.get('score', 0), breakdown, token.get('market_cap', 0), token.get('liquidity', 0), token.get('volume_24h', 0), token.get('price_usd', 0), token.get('holders', 0), token.get('buy_tax', 0), token.get('sell_tax', 0), token.get('source', ''), 1 if was_bought else 0)


async def save_scan_history_batch(tokens: list[dict], bought_addresses: set[str] | None = None):
    bought = bought_addresses or set()
    rows = []
    for t in tokens:
        breakdown = t.get('score_breakdown')
        if isinstance(breakdown, dict):
            breakdown = json.dumps(breakdown)
        rows.append((t.get('contract_address', ''), t.get('chain', ''), t.get('symbol', ''), t.get('name', ''), t.get('score', 0), breakdown, t.get('market_cap', 0), t.get('liquidity', 0), t.get('volume_24h', 0), t.get('price_usd', 0), t.get('holders', 0), t.get('buy_tax', 0), t.get('sell_tax', 0), t.get('source', ''), 1 if t.get('contract_address', '').lower() in bought else 0))
    if rows:
        await _executemany('INSERT INTO scan_history (contract_address, chain, symbol, name, score, score_breakdown, market_cap, liquidity, volume_24h, price_usd, holders, buy_tax, sell_tax, source, was_bought) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)


async def get_backtest_data(days: int = 7) -> dict:
    return {'summary': {}, 'score_ranges': [], 'trade_outcomes': [], 'simulations': []}


async def get_recent_detected_tokens(limit: int = 10) -> list[dict]:
    return await _fetch('SELECT * FROM detected_tokens ORDER BY detected_at DESC LIMIT ?', limit)


async def find_detected_token_by_prefix(token_prefix: str) -> dict | None:
    return await _fetchrow('SELECT contract_address, symbol FROM detected_tokens WHERE contract_address LIKE ? ORDER BY detected_at DESC LIMIT 1', token_prefix + '%')


async def add_snipe_target(token_address: str, user_id: int, amount: float = 0) -> bool:
    try:
        return _affected(await _execute('INSERT INTO snipe_targets (token_address, user_id, amount) VALUES (?, ?, ?) ON CONFLICT (token_address, user_id) DO NOTHING', token_address, user_id, amount)) > 0
    except Exception:
        return False


async def remove_snipe_target(token_address: str, user_id: int) -> bool:
    return _affected(await _execute('DELETE FROM snipe_targets WHERE token_address = ? AND user_id = ?', token_address, user_id)) > 0


async def get_active_snipe_targets() -> list[dict]:
    return await _fetch("SELECT * FROM snipe_targets WHERE status = 'active' ORDER BY added_at ASC")


async def get_user_snipe_targets(user_id: int) -> list[dict]:
    return await _fetch("SELECT * FROM snipe_targets WHERE user_id = ? AND status = 'active' ORDER BY added_at DESC", user_id)


async def mark_snipe_filled(token_address: str, user_id: int, tx_hash: str):
    await _execute("UPDATE snipe_targets SET status = 'filled', buy_tx_hash = ?, filled_at = NOW() WHERE token_address = ? AND user_id = ?", tx_hash, token_address, user_id)


_USER_SETTINGS_COLUMNS = frozenset({'min_score', 'stop_loss', 'take_profit', 'buy_percent', 'trailing_drop', 'slippage', 'max_positions', 'max_buy_amount', 'compound_enabled', 'compound_percent', 'tp1_percent', 'tp1_sell_percent', 'tp2_percent', 'trailing_sl_percent', 'daily_loss_limit_percent', 'capital_per_trade'})


async def get_user_settings(user_id: int) -> dict | None:
    return await _fetchrow('SELECT * FROM user_settings WHERE user_id = ?', user_id)


async def upsert_user_setting(user_id: int, key: str, value) -> bool:
    if key not in _USER_SETTINGS_COLUMNS:
        return False
    pool = await _ensure_pool()
    await pool.execute(f'INSERT INTO user_settings (user_id, {key}, updated_at) VALUES ($1, $2, NOW()) ON CONFLICT (user_id) DO UPDATE SET {key} = EXCLUDED.{key}, updated_at = NOW()', user_id, value)
    return True


async def delete_user_settings(user_id: int) -> bool:
    return _affected(await _execute('DELETE FROM user_settings WHERE user_id = ?', user_id)) > 0


async def add_to_blacklist(token_address: str, chain: str, reason: str, added_by: int) -> bool:
    try:
        return _affected(await _execute('INSERT INTO token_blacklist (token_address, chain, reason, added_by) VALUES (?, ?, ?, ?) ON CONFLICT (token_address, chain, added_by) DO NOTHING', token_address, chain, reason, added_by)) > 0
    except Exception:
        return False


async def remove_from_blacklist(token_address: str, chain: str, added_by: int | None = None) -> bool:
    if added_by is not None:
        return _affected(await _execute('DELETE FROM token_blacklist WHERE token_address = ? AND chain = ? AND added_by = ?', token_address, chain, added_by)) > 0
    return _affected(await _execute('DELETE FROM token_blacklist WHERE token_address = ? AND chain = ?', token_address, chain)) > 0


async def is_blacklisted(token_address: str, chain: str, *, user_id: int | None = None, admin_ids: list[int] | None = None) -> bool:
    if user_id is None:
        return await _fetchval('SELECT 1 FROM token_blacklist WHERE token_address = ? AND chain = ?', token_address, chain) is not None
    check_ids = [user_id] + [aid for aid in (admin_ids or []) if aid != user_id]
    placeholders = ','.join('?' * len(check_ids))
    return await _fetchval(f'SELECT 1 FROM token_blacklist WHERE token_address = ? AND chain = ? AND added_by IN ({placeholders})', token_address, chain, *check_ids) is not None


async def get_blacklist() -> list[dict]:
    return await _fetch('SELECT * FROM token_blacklist ORDER BY added_at DESC')


async def add_to_whitelist(token_address: str, chain: str, label: str, added_by: int) -> bool:
    try:
        return _affected(await _execute('INSERT INTO token_whitelist (token_address, chain, label, added_by) VALUES (?, ?, ?, ?) ON CONFLICT (token_address, chain) DO NOTHING', token_address, chain, label, added_by)) > 0
    except Exception:
        return False


async def remove_from_whitelist(token_address: str, chain: str) -> bool:
    return _affected(await _execute('DELETE FROM token_whitelist WHERE token_address = ? AND chain = ?', token_address, chain)) > 0


async def is_whitelisted(token_address: str, chain: str) -> bool:
    return await _fetchval('SELECT 1 FROM token_whitelist WHERE token_address = ? AND chain = ?', token_address, chain) is not None


async def get_whitelist() -> list[dict]:
    return await _fetch('SELECT * FROM token_whitelist ORDER BY added_at DESC')


async def create_dca_order(user_id: int, token_address: str, token_symbol: str, chain: str, total_amount: float, splits: int, interval_seconds: int) -> int | None:
    try:
        return await _fetchval('INSERT INTO dca_orders (user_id, token_address, token_symbol, chain, total_amount, amount_per_buy, splits_total, interval_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id', user_id, token_address, token_symbol, chain, total_amount, total_amount / splits, splits, interval_seconds)
    except Exception as exc:
        logger.error('create_dca_order error: %s', exc)
        return None


async def get_active_dca_orders() -> list[dict]:
    return await _fetch("SELECT * FROM dca_orders WHERE status = 'active' AND splits_done < splits_total AND (last_buy_at IS NULL OR last_buy_at <= NOW() - (interval_seconds || ' seconds')::interval)")


async def get_user_dca_orders(user_id: int) -> list[dict]:
    return await _fetch("SELECT * FROM dca_orders WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC", user_id)


async def advance_dca_order(order_id: int) -> bool:
    await _execute('UPDATE dca_orders SET splits_done = splits_done + 1, last_buy_at = NOW() WHERE id = ?', order_id)
    await _execute("UPDATE dca_orders SET status = 'completed' WHERE id = ? AND splits_done >= splits_total", order_id)
    return True


async def cancel_dca_order(order_id: int, user_id: int) -> bool:
    return _affected(await _execute("UPDATE dca_orders SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'", order_id, user_id)) > 0


async def create_limit_order(user_id: int, token_address: str, token_symbol: str, chain: str, side: str, amount: float, target_price: float) -> int | None:
    try:
        return await _fetchval('INSERT INTO limit_orders (user_id, token_address, token_symbol, chain, side, amount, target_price, condition) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id', user_id, token_address, token_symbol, chain, side, amount, target_price, 'lte' if side == 'buy' else 'gte')
    except Exception as exc:
        logger.error('create_limit_order error: %s', exc)
        return None


async def get_active_limit_orders() -> list[dict]:
    return await _fetch("SELECT * FROM limit_orders WHERE status = 'active'")


async def get_user_limit_orders(user_id: int) -> list[dict]:
    return await _fetch("SELECT * FROM limit_orders WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC", user_id)


async def fill_limit_order(order_id: int, tx_hash: str):
    await _execute("UPDATE limit_orders SET status = 'filled', fill_tx_hash = ?, filled_at = NOW() WHERE id = ?", tx_hash, order_id)


async def cancel_limit_order(order_id: int, user_id: int) -> bool:
    return _affected(await _execute("UPDATE limit_orders SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'", order_id, user_id)) > 0


async def get_effective_config(user_id: int) -> dict:
    from config import MIN_SCORE, STOP_LOSS, TAKE_PROFIT, BUY_PERCENT, TRAILING_DROP, SLIPPAGE, MAX_OPEN_POSITIONS, MAX_BUY_AMOUNT, COMPOUND_ENABLED, COMPOUND_PERCENT, TP1_PERCENT, TP1_SELL_PERCENT, TP2_PERCENT, TRAILING_SL_PERCENT, DAILY_LOSS_LIMIT_PCT
    defaults = {'min_score': MIN_SCORE, 'stop_loss': STOP_LOSS, 'take_profit': TAKE_PROFIT, 'buy_percent': BUY_PERCENT, 'trailing_drop': TRAILING_DROP, 'slippage': SLIPPAGE, 'max_positions': MAX_OPEN_POSITIONS, 'max_buy_amount': MAX_BUY_AMOUNT, 'compound_enabled': COMPOUND_ENABLED, 'compound_percent': COMPOUND_PERCENT, 'tp1_percent': TP1_PERCENT, 'tp1_sell_percent': TP1_SELL_PERCENT, 'tp2_percent': TP2_PERCENT, 'trailing_sl_percent': TRAILING_SL_PERCENT, 'daily_loss_limit_percent': DAILY_LOSS_LIMIT_PCT, 'capital_per_trade': None}
    user = await get_user_settings(user_id)
    if user:
        for key in defaults:
            if user.get(key) is not None:
                defaults[key] = user[key]
    return defaults


async def add_compound_funds(user_id: int, amount: float):
    await _execute('INSERT INTO compound_fund (user_id, available, total_compounded, updated_at) VALUES (?, ?, ?, NOW()) ON CONFLICT (user_id) DO UPDATE SET available = compound_fund.available + EXCLUDED.available, total_compounded = compound_fund.total_compounded + EXCLUDED.total_compounded, updated_at = NOW()', user_id, amount, amount)


async def get_compound_fund(user_id: int) -> float:
    return float(await _fetchval('SELECT available FROM compound_fund WHERE user_id = ?', user_id) or 0)


async def deduct_compound_funds(user_id: int, amount: float):
    await _execute('UPDATE compound_fund SET available = GREATEST(available - ?, 0), updated_at = NOW() WHERE user_id = ?', amount, user_id)


async def get_daily_pnl_report(user_id: int) -> dict | None:
    row = await _fetchrow("SELECT COUNT(*) as trades_today, COALESCE(SUM(CASE WHEN roi_percent > 0 THEN 1 ELSE 0 END), 0) as wins, COALESCE(SUM(CASE WHEN roi_percent <= 0 THEN 1 ELSE 0 END), 0) as losses, COALESCE(SUM(sell_amount_native - buy_amount_native), 0) as net_pnl FROM completed_trades WHERE user_id = ? AND closed_at >= CURRENT_DATE", user_id)
    pos = await _fetchrow('SELECT COUNT(*) as cnt, COALESCE(SUM(buy_amount_native), 0) as total_invested FROM open_positions WHERE user_id = ?', user_id)
    trades = row['trades_today'] if row else 0
    return {'trades_today': trades, 'wins': row.get('wins', 0), 'losses': row.get('losses', 0), 'win_rate': (row.get('wins', 0) / trades * 100) if trades else 0, 'net_pnl': row.get('net_pnl', 0), 'best_trade': None, 'worst_trade': None, 'open_count': pos.get('cnt', 0), 'total_invested': pos.get('total_invested', 0)}


async def get_pnl_report(user_id: int, days: int = 1) -> dict | None:
    row = await _fetchrow("SELECT COUNT(*) as trades, COALESCE(SUM(CASE WHEN roi_percent > 0 THEN 1 ELSE 0 END), 0) as wins, COALESCE(SUM(CASE WHEN roi_percent <= 0 THEN 1 ELSE 0 END), 0) as losses, COALESCE(SUM(sell_amount_native - buy_amount_native), 0) as net_pnl FROM completed_trades WHERE user_id = ? AND closed_at >= NOW() - ($2 || ' days')::interval", user_id, days)
    pos = await _fetchrow('SELECT COUNT(*) as cnt, COALESCE(SUM(buy_amount_native), 0) as total_invested FROM open_positions WHERE user_id = ?', user_id)
    trades = row['trades'] if row else 0
    return {'trades': trades, 'wins': row.get('wins', 0), 'losses': row.get('losses', 0), 'win_rate': (row.get('wins', 0) / trades * 100) if trades else 0, 'net_pnl': row.get('net_pnl', 0), 'best_trade': None, 'worst_trade': None, 'open_count': pos.get('cnt', 0), 'total_invested': pos.get('total_invested', 0)}


async def get_kill_switch_status(user_id: int) -> bool:
    return bool(await _fetchval('SELECT kill_switch_active FROM daily_loss_records WHERE user_id = ? AND date = ?', user_id, date.today().isoformat()))


async def activate_kill_switch(user_id: int):
    today = date.today().isoformat()
    loss = await get_daily_realized_loss(user_id)
    await _execute('INSERT INTO daily_loss_records (user_id, date, total_loss, kill_switch_active) VALUES (?, ?, ?, TRUE) ON CONFLICT (user_id, date) DO UPDATE SET total_loss = EXCLUDED.total_loss, kill_switch_active = TRUE', user_id, today, loss)
    await set_auto_trade(user_id, False)
    await _publish_event('kill_switch_triggered', {'user_id': user_id, 'date': today, 'total_loss': loss}, user_id)


async def deactivate_kill_switch(user_id: int):
    await _execute('INSERT INTO daily_loss_records (user_id, date, total_loss, kill_switch_active) VALUES (?, ?, 0, FALSE) ON CONFLICT (user_id, date) DO UPDATE SET kill_switch_active = FALSE', user_id, date.today().isoformat())


async def ensure_login_code_table():
    pool = await _ensure_pool()
    await pool.execute(_LOGIN_CODE_TABLE_DDL)


async def claim_login_code(code: str, user_id: int, username: str) -> str:
    """Attempt to claim a dashboard login code. Returns 'ok', 'invalid', 'expired', or 'already_claimed'."""
    await ensure_login_code_table()
    row = await _fetchrow('SELECT * FROM dashboard_login_codes WHERE code = ?', code)
    if not row:
        return 'invalid'
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    exp = row['expires_at']
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=tz.utc)
    if row.get('claimed_at') is not None:
        return 'already_claimed'
    if now > exp:
        return 'expired'
    status = await _execute(
        'UPDATE dashboard_login_codes SET user_id = ?, username = ?, claimed_at = NOW() WHERE code = ? AND claimed_at IS NULL',
        user_id, username, code,
    )
    return 'ok' if _affected(status) > 0 else 'already_claimed'
