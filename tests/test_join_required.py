"""Тесты модуля 'приведи друга' (join_required) и его фоновой проверки дедлайнов."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot import EventType, MessageContext
from bot import enforce_join_required_deadlines
from bot import JoinRequiredModule
from bot import JoinRequiredStateRepository
from bot import ChatSettingRepository


def _patch_db(monkeypatch, db_session, factory_holder) -> None:
    """
    Все компоненты (JoinRequiredModule, enforce_join_required_deadlines,
    и т.д.) используют session_scope() из bot.py, который открывает НОВУЮ
    сессию из глобального engine. Для теста с db_session-фикстурой (единая
    in-memory SQLite на тест) подменяем session_scope так, чтобы он
    возвращал ту же сессию, что и db_session — иначе тест увидит "разные
    базы" из-за in-memory-изоляции по соединению.
    """
    import bot as bot_module
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session_scope():
        yield db_session
        await db_session.commit()

    monkeypatch.setattr(bot_module, "session_scope", fake_session_scope)


@pytest.mark.asyncio
class TestJoinRequiredModule:
    async def test_join_sets_pending(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        module = JoinRequiredModule()

        ctx = MessageContext(
            chat_id=1, user_id=100, message_id=0, text=None, topic_id=None,
            created_at=datetime.now(timezone.utc), event_type=EventType.CHAT_MEMBER_JOINED,
        )
        result = await module.check(ctx)
        assert result is None

        state = await JoinRequiredStateRepository(db_session).get(1, 100)
        assert state is not None
        assert state.status == "pending"

    async def test_message_before_deadline_no_violation(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        module = JoinRequiredModule()

        await module.check(MessageContext(
            chat_id=1, user_id=100, message_id=0, text=None, topic_id=None,
            created_at=datetime.now(timezone.utc), event_type=EventType.CHAT_MEMBER_JOINED,
        ))
        result = await module.check(MessageContext(
            chat_id=1, user_id=100, message_id=1, text="hi", topic_id=None,
            created_at=datetime.now(timezone.utc), event_type=EventType.NEW_MESSAGE,
        ))
        assert result is None

    async def test_inviting_friend_marks_satisfied(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        module = JoinRequiredModule()

        await module.check(MessageContext(
            chat_id=1, user_id=100, message_id=0, text=None, topic_id=None,
            created_at=datetime.now(timezone.utc), event_type=EventType.CHAT_MEMBER_JOINED,
        ))
        await module.check(MessageContext(
            chat_id=1, user_id=100, message_id=0, text=None, topic_id=None,
            created_at=datetime.now(timezone.utc), event_type=EventType.NEW_CHAT_MEMBERS,
        ))

        state = await JoinRequiredStateRepository(db_session).get(1, 100)
        assert state.status == "satisfied"

    async def test_deadline_exceeded_triggers_violation_as_safety_net(
        self, db_session, monkeypatch
    ) -> None:
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        module = JoinRequiredModule()
        state_repo = JoinRequiredStateRepository(db_session)

        await state_repo.set_pending(1, 200)
        state = await state_repo.get(1, 200)
        state.joined_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        await db_session.flush()

        result = await module.check(MessageContext(
            chat_id=1, user_id=200, message_id=3, text="hi again", topic_id=None,
            created_at=datetime.now(timezone.utc), event_type=EventType.NEW_MESSAGE,
        ))
        assert result is not None
        assert result.rule_name == "deadline_exceeded"

    async def test_rejoin_resets_to_pending(self, db_session, monkeypatch) -> None:
        """Правило сбрасывается при повторном входе после выхода."""
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        module = JoinRequiredModule()
        state_repo = JoinRequiredStateRepository(db_session)

        await state_repo.set_pending(1, 200)
        state = await state_repo.get(1, 200)
        state.joined_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        state.status = "satisfied"  # представим, что пользователь выполнил условие, потом вышел
        await db_session.flush()

        await module.check(MessageContext(
            chat_id=1, user_id=200, message_id=0, text=None, topic_id=None,
            created_at=datetime.now(timezone.utc), event_type=EventType.CHAT_MEMBER_JOINED,
        ))

        refreshed = await state_repo.get(1, 200)
        assert refreshed.status == "pending"
        assert refreshed.joined_at > datetime.now(timezone.utc) - timedelta(seconds=10)


@pytest.mark.asyncio
class TestEnforceJoinRequiredDeadlines:
    async def test_punishes_timed_out_silent_user(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        state_repo = JoinRequiredStateRepository(db_session)

        await state_repo.set_pending(1, 100)
        state = await state_repo.get(1, 100)
        state.joined_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        await db_session.flush()

        bot = AsyncMock()
        count = await enforce_join_required_deadlines(bot)
        assert count == 1
        bot.ban_chat_member.assert_awaited_once()

    async def test_does_not_punish_user_within_deadline(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        await JoinRequiredStateRepository(db_session).set_pending(1, 200)

        bot = AsyncMock()
        count = await enforce_join_required_deadlines(bot)
        assert count == 0
        bot.ban_chat_member.assert_not_awaited()

    async def test_does_not_double_punish_on_second_run(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session, None)
        await ChatSettingRepository(db_session).set_enabled(1, "join_required", True)
        state_repo = JoinRequiredStateRepository(db_session)
        await state_repo.set_pending(1, 100)
        state = await state_repo.get(1, 100)
        state.joined_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        await db_session.flush()

        bot = AsyncMock()
        first = await enforce_join_required_deadlines(bot)
        second = await enforce_join_required_deadlines(bot)
        assert first == 1
        assert second == 0

    async def test_disabled_module_is_skipped(self, db_session, monkeypatch) -> None:
        _patch_db(monkeypatch, db_session, None)
        # Модуль НЕ включён для чата 1.
        await JoinRequiredStateRepository(db_session).set_pending(1, 100)
        state = await JoinRequiredStateRepository(db_session).get(1, 100)
        state.joined_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        await db_session.flush()

        bot = AsyncMock()
        count = await enforce_join_required_deadlines(bot)
        assert count == 0
        bot.ban_chat_member.assert_not_awaited()
