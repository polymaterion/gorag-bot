"""Тесты фоновых периодических задач: очистка истории и join_required enforcer loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot import run_join_required_enforcer_loop
from bot import MessageHistoryRepository
from bot import JoinRequiredStateRepository
from bot import ChatSettingRepository
from bot import cleanup_message_history_once, run_message_history_cleanup_loop


def _patch_db(monkeypatch, db_session) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session_scope():
        yield db_session
        await db_session.commit()

    monkeypatch.setattr("bot.session_scope", fake_session_scope)


@pytest.mark.asyncio
class TestMessageHistoryCleanup:
    async def test_cleanup_once_removes_old_entries(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session)
        history_repo = MessageHistoryRepository(db_session)
        await history_repo.add(
            chat_id=1, user_id=2, message_id=10, text_normalized="old message", topic_id=None
        )
        # "Состариваем" запись напрямую в БД.
        from bot import MessageHistory
        from sqlalchemy import select

        result = await db_session.execute(select(MessageHistory))
        entry = result.scalar_one()
        entry.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        await db_session.flush()

        deleted = await cleanup_message_history_once()
        assert deleted == 1

    async def test_cleanup_loop_stops_on_event(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session)
        stop_event = asyncio.Event()

        task = asyncio.create_task(run_message_history_cleanup_loop(stop_event))
        await asyncio.sleep(0.05)
        stop_event.set()
        # Не должно зависнуть — таймаут перестраховка для самого теста.
        await asyncio.wait_for(task, timeout=3)
        assert task.done()

    async def test_cleanup_loop_survives_internal_errors(self, db_session, monkeypatch) -> None:
        """Ошибка в одной итерации не должна останавливать цикл (только логируется)."""
        call_count = {"n": 0}

        async def failing_cleanup():
            call_count["n"] += 1
            raise RuntimeError("boom")

        monkeypatch.setattr("bot.cleanup_message_history_once", failing_cleanup)
        # Ускоряем интервал для теста.
        from bot import get_settings

        monkeypatch.setenv("HISTORY_CLEANUP_INTERVAL_SECONDS", "0")
        get_settings.cache_clear()

        stop_event = asyncio.Event()
        task = asyncio.create_task(run_message_history_cleanup_loop(stop_event))
        await asyncio.sleep(0.2)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3)

        assert call_count["n"] >= 1  # цикл действительно вызывался несмотря на ошибки
        get_settings.cache_clear()


@pytest.mark.asyncio
class TestJoinRequiredEnforcerLoop:
    async def test_enforcer_loop_stops_on_event(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session)
        bot = AsyncMock()
        stop_event = asyncio.Event()

        task = asyncio.create_task(
            run_join_required_enforcer_loop(bot, stop_event, interval_seconds=10)
        )
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3)
        assert task.done()

    async def test_enforcer_loop_punishes_timed_out_user(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        state_repo = JoinRequiredStateRepository(db_session)
        await state_repo.set_pending(1, 100)
        state = await state_repo.get(1, 100)
        state.joined_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        await db_session.flush()

        bot = AsyncMock()
        stop_event = asyncio.Event()

        task = asyncio.create_task(
            run_join_required_enforcer_loop(bot, stop_event, interval_seconds=10)
        )
        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3)

        bot.ban_chat_member.assert_awaited()
