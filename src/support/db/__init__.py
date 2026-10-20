from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from support.config import settings

# Векторы pgvector передаются текстом «[0.1,0.2,…]»: так их кодирует тип Vector из pgvector.sqlalchemy
engine = create_async_engine(settings.database_url, pool_size=10, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

__all__ = ["AsyncSession", "Session", "engine"]
