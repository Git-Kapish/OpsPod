from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def make_sessionmaker(url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        create_async_engine(url, pool_pre_ping=True, pool_size=20, max_overflow=20),
        expire_on_commit=False,
    )
