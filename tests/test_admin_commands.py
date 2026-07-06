"""
Тесты admin-команд. Хэндлеры aiogram вызываются напрямую (не через полный
Dispatcher) с мок-объектами Message/Bot — стандартный подход для unit-
тестирования aiogram 3.x хэндлеров без поднятия реального polling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Chat, ChatMemberMember, ChatMemberOwner, User

import bot as admin_commands
from bot import TranslationLoader
from bot import (
    WhitelistedLinkRepository,
    WhitelistedLinkSenderRepository,
)
from bot import ChatSettingRepository
from bot import BannedWordRepository


@pytest.fixture
def translator() -> TranslationLoader:
    t = TranslationLoader("locales", fallback_locale="en")
    t.load()
    return t


def _owner_bot(uid: int = 1) -> AsyncMock:
    bot = AsyncMock()
    bot.get_chat_member.return_value = ChatMemberOwner(
        status="creator", user=User(id=uid, is_bot=False, first_name="Admin"), is_anonymous=False
    )
    return bot


def _member_bot(uid: int = 2) -> AsyncMock:
    bot = AsyncMock()
    bot.get_chat_member.return_value = ChatMemberMember(
        status="member", user=User(id=uid, is_bot=False, first_name="Regular")
    )
    return bot


def _message(chat_id: int, user_id: int, chat_type: str = "supergroup") -> MagicMock:
    message = MagicMock()
    message.chat = Chat(id=chat_id, type=chat_type)
    message.from_user = User(id=user_id, is_bot=False, first_name="User")
    message.answer = AsyncMock()
    message.reply_to_message = None
    return message


def _patch_db(monkeypatch, db_session) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session_scope():
        yield db_session
        await db_session.commit()

    monkeypatch.setattr("bot.session_scope", fake_session_scope)


@pytest.mark.asyncio
class TestAntispamToggle:
    async def test_admin_can_enable_antispam(self, db_session, monkeypatch, translator) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)

        await admin_commands.cmd_antispam_on(message, bot, translator, "ru")

        message.answer.assert_awaited_once()
        setting = await ChatSettingRepository(db_session).get(100, "antispam")
        assert setting is not None
        assert setting.enabled is True

    async def test_regular_member_cannot_enable_antispam(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _member_bot()
        message = _message(100, 2)

        await admin_commands.cmd_antispam_on(message, bot, translator, "ru")

        setting = await ChatSettingRepository(db_session).get(100, "antispam")
        assert setting is None  # никакого побочного эффекта

    async def test_command_outside_group_rejected(self, db_session, monkeypatch, translator) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1, chat_type="private")

        await admin_commands.cmd_antispam_on(message, bot, translator, "ru")

        setting = await ChatSettingRepository(db_session).get(100, "antispam")
        assert setting is None
        bot.get_chat_member.assert_not_called()


@pytest.mark.asyncio
class TestWhitelistLinksCommands:
    async def test_add_link_to_whitelist(self, db_session, monkeypatch, translator) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="antispam_links_add", args="example.com")

        await admin_commands.cmd_links_add(message, bot, cmd, translator, "ru")

        items = await WhitelistedLinkRepository(db_session).list_for_chat(100)
        assert len(items) == 1
        assert items[0].value == "example.com"

    async def test_add_link_without_args_shows_usage(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="antispam_links_add", args=None)

        await admin_commands.cmd_links_add(message, bot, cmd, translator, "ru")

        items = await WhitelistedLinkRepository(db_session).list_for_chat(100)
        assert items == []

    async def test_remove_nonexistent_link_reports_not_found(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="antispam_links_remove", args="nope.com")

        await admin_commands.cmd_links_remove(message, bot, cmd, translator, "ru")

        message.answer.assert_awaited_once()


@pytest.mark.asyncio
class TestBannedWordsCommands:
    async def test_add_word(self, db_session, monkeypatch, translator) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="antispam_words_add", args="плохое слово")

        await admin_commands.cmd_words_add(message, bot, cmd, translator, "ru")

        items = await BannedWordRepository(db_session).list_for_chat(100)
        assert len(items) == 1
        assert items[0].phrase_original == "плохое слово"

    async def test_remove_word(self, db_session, monkeypatch, translator) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)

        add_cmd = CommandObject(prefix="/", command="antispam_words_add", args="badword")
        await admin_commands.cmd_words_add(message, bot, add_cmd, translator, "ru")

        remove_cmd = CommandObject(prefix="/", command="antispam_words_remove", args="badword")
        await admin_commands.cmd_words_remove(message, bot, remove_cmd, translator, "ru")

        items = await BannedWordRepository(db_session).list_for_chat(100)
        assert items == []


@pytest.mark.asyncio
class TestSetPunishment:
    async def test_set_punishment_for_antispam_submodule(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(
            prefix="/", command="set_punishment", args="antispam links ban_permanent"
        )

        await admin_commands.cmd_set_punishment(message, bot, cmd, translator, "ru")

        setting = await ChatSettingRepository(db_session).get(100, "antispam")
        assert setting.config["links"]["punishment"] == "ban_permanent"

    async def test_set_punishment_for_top_level_module(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="set_punishment", args="join_required rule kick")

        await admin_commands.cmd_set_punishment(message, bot, cmd, translator, "ru")

        setting = await ChatSettingRepository(db_session).get(100, "join_required")
        assert setting.punishment == "kick"

    async def test_invalid_punishment_value_rejected(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(
            prefix="/", command="set_punishment", args="antispam links not_a_real_punishment"
        )

        await admin_commands.cmd_set_punishment(message, bot, cmd, translator, "ru")

        setting = await ChatSettingRepository(db_session).get(100, "antispam")
        # get_or_create ещё не вызывался внутри invalid-ветки, поэтому
        # настройки для чата не должно быть вовсе.
        assert setting is None

    async def test_malformed_args_shows_usage(self, db_session, monkeypatch, translator) -> None:
        _patch_db(monkeypatch, db_session)
        bot = _owner_bot()
        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="set_punishment", args="onlyonepart")

        await admin_commands.cmd_set_punishment(message, bot, cmd, translator, "ru")
        message.answer.assert_awaited_once()


@pytest.mark.asyncio
class TestScamlistCommands:
    async def test_global_admin_can_add_scammer(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        monkeypatch.setenv("BOT_TOKEN", "test:token")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("GLOBAL_ADMIN_IDS", "1")
        from bot import get_settings

        get_settings.cache_clear()

        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="scamlist_add", args="555")

        await admin_commands.cmd_scamlist_add(message, cmd, translator, "ru")

        from bot import ScammerRepository

        assert await ScammerRepository(db_session).is_scammer(555) is True
        get_settings.cache_clear()

    async def test_non_global_admin_cannot_add_scammer(
        self, db_session, monkeypatch, translator
    ) -> None:
        _patch_db(monkeypatch, db_session)
        monkeypatch.setenv("BOT_TOKEN", "test:token")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("GLOBAL_ADMIN_IDS", "999")  # не совпадает с message.from_user.id
        from bot import get_settings

        get_settings.cache_clear()

        message = _message(100, 1)
        cmd = CommandObject(prefix="/", command="scamlist_add", args="555")

        await admin_commands.cmd_scamlist_add(message, cmd, translator, "ru")

        from bot import ScammerRepository

        assert await ScammerRepository(db_session).is_scammer(555) is False
        get_settings.cache_clear()
