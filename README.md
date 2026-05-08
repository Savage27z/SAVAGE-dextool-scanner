# SAVAGE Dextool Scanner

SAVAGE is a Solana-first trading bot that scans low-cap tokens, scores risk, executes buys/sells, monitors open positions, tracks whales, supports sniper/DCA/limit orders, and sends Telegram alerts. This version upgrades persistence from local SQLite to PostgreSQL and adds a production FastAPI dashboard backend backed by Redis for price caching and WebSocket event fan-out.

## Architecture

```text
Telegram Bot ─────┐
                  │ reads/writes
                  ▼
              PostgreSQL ◄──── FastAPI Backend ◄──── React Frontend
                  ▲                 ▲   │
                  │                 │   └── WebSocket /ws
                  │                 │
             Alembic migrations     │
                                    ▼
                         Redis cache + pub/sub
```

- `bot.py`, `scanner.py`, `monitor.py`, `trader.py`, `sniper.py`, `whale_tracker.py`, and related modules remain at the repository root.
- Root `db.py` uses `asyncpg` and a shared PostgreSQL connection pool.
- `backend/` is a separate FastAPI process for dashboard auth, overview, positions, trade history, performance, settings, wallet, and WebSocket APIs.
- Redis stores 30-second price cache entries and forwards bot trade events to connected dashboard clients.
- Alembic owns schema creation and migrations.

## Folder structure

```text
SAVAGE-dextool-scanner/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── models.py
│   │   ├── schemas.py
│   │   ├── auth.py
│   │   ├── deps.py
│   │   ├── redis_client.py
│   │   ├── ws_manager.py
│   │   └── routes/
│   ├── requirements.txt
│   └── Dockerfile
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/001_initial_schema.py
├── frontend/Dockerfile
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
├── db.py
├── monitor.py
└── existing root bot modules
```

## Environment variables

| Variable | Required | Description | Example |
|---|---:|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token used by bot and login verification | `123:abc` |
| `TELEGRAM_BOT_NAME` | dashboard | Bot username for Telegram Login Widget | `savage_bot` |
| `TELEGRAM_CHAT_ID` | yes | Admin Telegram user/chat ID | `123456789` |
| `ADMIN_TELEGRAM_IDS` | dashboard | Comma-separated admin Telegram user IDs | `123456789,987654321` |
| `PRIVATE_KEY` | bot | Admin trading private key | `base58...` |
| `ENCRYPTION_KEY` | yes | Fernet key for stored user wallets | `python -c ...` |
| `DATABASE_URL` | yes | PostgreSQL connection URL | `postgresql://savage:savage@localhost:5432/savage_trading` |
| `POSTGRES_PASSWORD` | Docker | PostgreSQL container password | `savage` |
| `REDIS_URL` | yes | Redis URL | `redis://localhost:6379/0` |
| `JWT_SECRET` | dashboard | JWT signing secret | long random string |
| `JWT_EXPIRE_HOURS` | dashboard | Dashboard session lifetime | `72` |
| `FRONTEND_URL` | backend | CORS origin for local frontend | `http://localhost:5173` |
| `FRONTEND_PORT` | Docker | Published frontend port | `3000` |
| `BACKEND_PORT` | Docker | Published backend port | `8000` |
| `VITE_API_URL` / `FRONTEND_API_URL` | frontend | Browser API base URL; Vercel proxy also reads `FRONTEND_API_URL` or `BACKEND_URL` | `https://your-backend.up.railway.app` |
| `VITE_WS_URL` / `FRONTEND_WS_URL` | frontend | Browser WebSocket URL | `wss://your-backend.up.railway.app` |
| `RPC_URL_SOL` | bot/backend | Solana RPC endpoint | `https://api.mainnet-beta.solana.com` |
| `RPC_URL_ETH` | optional | Ethereum RPC endpoint | Infura/Alchemy URL |
| `RPC_URL_BSC` | optional | BSC RPC endpoint | public/private RPC |
| `DEXTOOLS_API_KEY` | optional | DexTools API key | empty allowed |
| `DEXTOOLS_PLAN` | optional | DexTools plan path | `trial` |
| `BIRDEYE_API_KEY` | optional | Birdeye market data key | empty allowed |
| `HELIUS_API_KEY` | optional | Helius key for Pump.fun/RPC features | empty allowed |
| `CHAIN` | bot | Active trading chain | `SOL` |
| `BUY_PERCENT` | bot | Wallet percent per buy | `50` |
| `TAKE_PROFIT` | bot | Legacy take-profit percent | `20` |
| `STOP_LOSS` | bot | Negative ROI stop-loss threshold | `-30` |
| `TRAILING_ENABLED` | bot | Enable trailing logic | `true` |
| `TRAILING_DROP` | bot | Legacy trailing drop percent | `10` |
| `SLIPPAGE` | bot | Swap slippage percent | `15` |
| `SELL_TIERS` | bot | Legacy tiered sell config | `50:50,100:25` |
| `TP1_PERCENT` | bot/dashboard | TP1 ROI threshold | `50` |
| `TP1_SELL_PERCENT` | bot/dashboard | Position percent sold at TP1 | `50` |
| `TP2_PERCENT` | bot/dashboard | TP2 ROI threshold for remaining position | `100` |
| `TRAILING_SL_PERCENT` | bot/dashboard | Trailing stop from peak | `15` |
| `DAILY_LOSS_LIMIT_PCT` | bot/dashboard | Daily loss kill-switch percent | `20` |
| `MIN_LIQUIDITY` | bot | Minimum liquidity filter | `5000` |
| `MAX_MCAP` | bot | Maximum market cap filter | `500000` |
| `MIN_MCAP` | bot | Minimum market cap filter | `10000` |
| `MIN_SCORE` | bot | Minimum token safety score | `40` |
| `MAX_OPEN_POSITIONS` | bot | Max open positions per user | `5` |
| `MAX_DAILY_LOSS` | bot | Legacy native-token daily loss cap | `2.0` |
| `MAX_BUY_AMOUNT` | bot | Max native buy amount | `1.0` |
| `COMPOUND_ENABLED` | bot | Enable profit compounding | `false` |
| `COMPOUND_PERCENT` | bot | Profit percent sent to compound fund | `50` |
| `SCAN_INTERVAL` | bot | Scanner interval seconds | `60` |
| `MONITOR_INTERVAL` | bot | Position monitor interval seconds | `30` |
| `WHALE_TRACKING_ENABLED` | bot | Enable whale monitoring | `true` |
| `WHALE_CHECK_INTERVAL` | bot | Whale polling interval | `45` |
| `WHALE_MIN_SOL` | bot | Minimum SOL spend alert | `1.0` |
| `WHALE_COPY_ENABLED` | bot | Enable whale copy trading | `false` |
| `WHALE_COPY_AMOUNT` | bot | Copy buy size in SOL | `0.1` |
| `WHALE_COPY_MAX_PER_TOKEN` | bot | Copy cap per token | `1` |
| `ANTIRUG_ENABLED` | bot | Enable liquidity emergency exits | `true` |
| `ANTIRUG_MIN_LIQ` | bot | Minimum liquidity floor | `1000` |
| `ANTIRUG_LIQ_DROP_PCT` | bot | Entry liquidity drop trigger | `70` |
| `OPERATOR_FEE_ENABLED` | bot | Enable profit fee collection | `true` |
| `OPERATOR_FEE_PCT` | bot | Fee percent of winning trades | `5` |
| `API_ENABLED` | legacy | Legacy root API toggle | `false` |
| `API_PORT` | legacy | Legacy API port | `8080` |
| `API_KEY` | legacy | Legacy API key | random string |
| `ALERT_BROADCAST` | bot | Broadcast detections to all chats | `true` |
| `AUTO_START_SCANNER` | bot | Start scanner automatically when bot boots | `true` |
| `SNIPER_ENABLED` | bot | Enable sniper loop | `false` |
| `SNIPER_CHECK_INTERVAL` | bot | Sniper interval seconds | `10` |
| `SNIPER_MIN_LIQUIDITY` | bot | Sniper liquidity floor | `1000` |

## Local setup with Docker Compose

1. Copy the environment file and fill required secrets:

   ```bash
   cp .env.example .env
   ```

2. Start PostgreSQL, Redis, migrations, bot, backend, and placeholder frontend:

   ```bash
   docker compose up --build
   ```

3. Verify the backend:

   ```bash
   curl http://localhost:8000/api/health
   ```

4. Backend API runs at `http://localhost:8000`; frontend placeholder is published on `http://localhost:3000` until the React app is added.

## Manual setup without Docker

1. Install PostgreSQL 16 and Redis 7 locally.
2. Create the database:

   ```bash
   createdb savage_trading
   createuser savage
   psql -c "ALTER USER savage WITH PASSWORD 'savage';"
   psql -c "GRANT ALL PRIVILEGES ON DATABASE savage_trading TO savage;"
   ```

3. Install bot dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Install backend dependencies:

   ```bash
   pip install -r backend/requirements.txt
   ```

5. Run migrations:

   ```bash
   export DATABASE_URL=postgresql://savage:savage@localhost:5432/savage_trading
   alembic upgrade head
   ```

6. Start services in separate shells:

   ```bash
   python bot.py
   uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
   ```

## Railway deployment

1. Create a Railway project from this repository.
2. Add PostgreSQL and Redis addons.
3. Set required variables from `.env.example` in Railway variables.
4. Ensure `DATABASE_URL` and `REDIS_URL` point to the Railway addon values.
5. Use `railway.backend.toml` for the dashboard backend service and set `FRONTEND_URL` to the Vercel URL.
6. Use `railway.toml` for the Telegram bot/scanner service; it runs migrations and starts `bot.py`.

## User registration and dashboard login

1. Admin starts the Telegram bot.
2. Admin allows a user with the bot command `/adduser <telegram_user_id> [username]`.
3. The user opens the dashboard frontend.
4. The frontend Telegram Login Widget sends signed Telegram login data to `POST /api/auth/telegram`.
5. The backend verifies the HMAC signature, checks `allowed_users`, issues a JWT, and stores it in an HTTP-only `auth_token` cookie.
6. Dashboard requests use the cookie or `Authorization: Bearer <token>`.

## Dashboard API features

- `GET /api/health`: unauthenticated health check.
- `POST /api/auth/telegram`, `GET /api/auth/me`, `POST /api/auth/logout`: Telegram Login and JWT session management.
- `GET /api/overview`: wallet value, PnL, win rate, active positions, kill-switch state, auto-trade state.
- `GET /api/positions`: open positions with cached current price, unrealised PnL, TP1/TP2 levels, trailing stop level.
- `POST /api/positions/{id}/close`: manual close flow and trade recording.
- `GET /api/trades`: paginated completed trade history.
- `GET /api/trades/export`: CSV export.
- `GET /api/performance`: win rate, average ROI, best/worst trade, cumulative PnL chart data.
- `GET/PUT /api/settings`: per-user trading settings including TP1, TP2, trailing stop, daily kill switch, max positions.
- `POST/DELETE /api/settings/blacklist`: token blacklist management.
- `PUT /api/settings/auto-trade`: enable or pause auto-trading.
- `GET /api/wallet`: public wallet address and SOL/USD balance only; private keys and seed phrases are never returned.
- `WS /ws`: authenticated real-time channel for `price_update`, `position_update`, `trade_closed`, `kill_switch_triggered`, and `new_token_detected`.

## Trading logic changes

- TP1 sells a configurable percentage of a position at `TP1_PERCENT` and marks `open_positions.tp1_hit`.
- TP2 closes the remaining position after TP1 when ROI reaches `TP2_PERCENT`.
- Trailing stop-loss tracks `peak_price` and closes when price falls by `TRAILING_SL_PERCENT` from the peak.
- Daily loss kill switch records `daily_loss_records`, disables `user_wallets.auto_trade`, and notifies the user when realised daily losses exceed `DAILY_LOSS_LIMIT_PCT`.
