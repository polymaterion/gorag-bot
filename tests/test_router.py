"""
Тесты основного модерационного router. Проверяют, что pipeline реально
триггерит наказание через executor и записывает moderation_log — то есть
всю цепочку "aiogram Message -> MessageContext -> pipeline -> executor".
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, User

import bot as router_module
from bot import ModuleRegistry
from bot import AntispamModule
from bot import ModerationLogRepository
from bot import ChatSettingRepository
from bot import BannedWordRepository


def _message(
    chat_id: int,
    user_id: int,
    text: str | None,
    message_id: int = 1,
    new_chat_members: list | None = None,
) -> MagicMock:
    message = MagicMock()
    message.chat = Chat(id=chat_id, type="supergroup")
    message.from_user = User(id=user_id, is_bot=False, first_name="User")
    message.text = text
    message.caption = None
    message.message_id = message_id
    message.message_thread_id = None
    message.date = datetime.now(timezone.utc)
    message.new_chat_members = new_chat_members
    return message


def _patch_db_and_registry(monkeypatch, db_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session_scope():
        yield db_session
        await db_session.commit()

    monkeypatch.setattr("bot.session_scope", fake_session_scope)

    test_registry = ModuleRegistry()
    test_registry.register(AntispamModule())
    monkeypatch.setattr(router_module, "_pipeline_runner", _make_runner(test_registry))


def _make_runner(registry):
    from bot import PipelineRunner

    return PipelineRunner(registry)


@pytest.fixture
def translator():
    from bot import TranslationLoader

    t = TranslationLoader("locales", fallback_locale="en")
    t.load()
    return t


@pytest.mark.asyncio
class TestOnMessage:
    async def test_banned_word_triggers_delete_and_log(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db_and_registry(monkeypatch, db_session)
        await ChatSettingRepository(db_session).set_enabled(100, "antispam", True)
        await BannedWordRepository(db_session).add(100, "спам", "спам", added_by=1)

        bot = AsyncMock()
        message = _message(100, 5, "это спам сообщение", message_id=42)

        await router_module.on_message(message, bot, translator, "ru")

        bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)
        logs = await ModerationLogRepository(db_session).list_for_chat(100)
        assert len(logs) == 1
        assert logs[0].event_type == "antispam.words"

    async def test_clean_message_no_action(self, db_session, monkeypatch, translator) -> None:
        _patch_db_and_registry(monkeypatch, db_session)
        await ChatSettingRepository(db_session).set_enabled(100, "antispam", True)

        bot = AsyncMock()
        message = _message(100, 5, "привет как дела")

        await router_module.on_message(message, bot, translator, "ru")

        bot.delete_message.assert_not_awaited()
        bot.ban_chat_member.assert_not_awaited()

    async def test_command_message_ignored(self, db_session, monkeypatch, translator) -> None:
        _patch_db_and_registry(monkeypatch, db_session)
        await ChatSettingRepository(db_session).set_enabled(100, "antispam", True)
        await BannedWordRepository(db_session).add(100, "settings", "settings", added_by=1)

        bot = AsyncMock()
        # Даже если бы "/settings" совпало с banned word "settings" внутри
        # текста, команда должна быть проигнорирована pipeline'ом целиком.
        message = _message(100, 5, "/settings")

        await router_module.on_message(message, bot, translator, "ru")

        bot.delete_message.assert_not_awaited()

    async def test_private_chat_ignored(self, db_session, monkeypatch, translator) -> None:
        _patch_db_and_registry(monkeypatch, db_session)
        bot = AsyncMock()
        message = _message(100, 5, "любой текст")
        message.chat = Chat(id=100, type="private")

        await router_module.on_message(message, bot, translator, "ru")

        bot.delete_message.assert_not_awaited()

    async def test_disabled_antispam_no_action(self, db_session, monkeypatch, translator) -> None:
        _patch_db_and_registry(monkeypatch, db_session)
        # antispam НЕ включён для чата
        await BannedWordRepository(db_session).add(100, "спам", "спам", added_by=1)

        bot = AsyncMock()
        message = _message(100, 5, "это спам сообщение")

        await router_module.on_message(message, bot, translator, "ru")

        bot.delete_message.assert_not_awaited()

    async def test_handler_never_raises_on_internal_error(
        self, db_session, monkeypatch, translator
    ) -> None:
        """Даже если что-то внутри пайплайна упадёт, хэндлер не должен пробрасывать исключение."""
        _patch_db_and_registry(monkeypatch, db_session)

        bot = AsyncMock()
        message = _message(100, 5, "текст")
        # Ломаем bot.delete_message, чтобы спровоцировать ошибку внутри
        # apply_punishment — хэндлер всё равно не должен упасть.
        bot.delete_message.side_effect = RuntimeError("unexpected")

        await ChatSettingRepository(db_session).set_enabled(100, "antispam", True)
        await BannedWordRepository(db_session).add(100, "текст", "текст", added_by=1)

        # Не должно бросить исключение наружу.
        await router_module.on_message(message, bot, translator, "ru")
