import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import FRONTEND_URL, REDIS_URL
from .redis_client import redis_pool
from .ws_manager import ws_manager
from .routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('SAVAGE dashboard starting up')
    await redis_pool.connect(REDIS_URL)
    if redis_pool.client:
        await ws_manager.start_redis_listener()
    try:
        from .database import engine
        from sqlalchemy import text
        if engine is not None:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS dashboard_login_codes (
                        code VARCHAR(10) PRIMARY KEY,
                        user_id BIGINT,
                        username VARCHAR(255),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        expires_at TIMESTAMPTZ NOT NULL,
                        claimed_at TIMESTAMPTZ,
                        consumed_at TIMESTAMPTZ
                    )
                """))
    except Exception as exc:
        logger.warning('Failed to ensure dashboard_login_codes table at backend startup: %s', exc)
    logger.info('Lifespan startup complete – ready to serve')
    yield
    await ws_manager.stop_redis_listener()
    await redis_pool.close()


app = FastAPI(title='SAVAGE Trading Dashboard', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, 'http://localhost:5173', 'http://localhost:3000'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)
app.include_router(router, prefix='/api')


@app.get('/api/health')
async def health():
    return {'status': 'ok'}


@app.get('/')
async def root():
    return {'status': 'ok', 'docs': '/docs', 'health': '/api/health'}
