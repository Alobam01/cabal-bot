import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
from dotenv import load_dotenv
from models import Base

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://") :]
if DATABASE_URL.startswith("postgresql://") and "postgresql+asyncpg://" not in DATABASE_URL:
    DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgresql://") :]

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        result = await conn.execute(
            text(
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'user_configs'
                  AND column_name = 'telegram_id'
                """
            )
        )
        row = result.first()
        if row and row[0] == "integer":
            await conn.execute(
                text(
                    """
                    ALTER TABLE user_configs
                    ALTER COLUMN telegram_id
                    TYPE BIGINT
                    USING telegram_id::bigint
                    """
                )
            )
    print("✅ Neon Postgres ready")
