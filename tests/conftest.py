"""
Общие pytest-фикстуры. Для unit-тестов используем in-memory SQLite вместо
реального Postgres — быстрее, не требует поднятого контейнера с БД.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Весь код проекта лежит в одном файле bot.py в корне репозитория —
# добавляем корень в sys.path, чтобы `import bot` резолвился при запуске
# pytest из любого cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot import Base  # noqa: E402


@pytest.fixture(autouse=True)
def _required_env_vars(monkeypatch):
    """
    BOT_TOKEN и DATABASE_URL обязательны для Settings (pydantic-settings),
    и любой код, вызывающий get_settings(), упадёт без них. Задаём
    безопасные тестовые значения по умолчанию для КАЖДОГО теста; тесты,
    которым нужны конкретные значения (например, GLOBAL_ADMIN_IDS),
    переопределяют их через свой monkeypatch.setenv и должны звать
    get_settings.cache_clear().
    """
    monkeypatch.setenv("BOT_TOKEN", os.environ.get("BOT_TOKEN", "test:token"))
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db"),
    )
    yield
    from bot import get_settings

    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session

    await engine.dispose()
