"""Тесты применения наказаний через Bot API (с мок-ботом, без реальных запросов)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot import Punishment
from bot import apply_punishment


@pytest.mark.asyncio
class TestApplyPunishment:
    async def test_none_punishment_is_noop(self) -> None:
        bot = AsyncMock()
        ok = await apply_punishment(
            bot=bot, chat_id=1, user_id=2, punishment=Punishment.NONE, message_id=None
        )
        assert ok is True
        bot.delete_message.assert_not_awaited()
        bot.ban_chat_member.assert_not_awaited()

    async def test_delete_message_happy_path(self) -> None:
        bot = AsyncMock()
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.DELETE_MESSAGE,
            message_id=100,
        )
        assert ok is True
        bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=100)

    async def test_delete_message_without_message_id_fails_gracefully(self) -> None:
        bot = AsyncMock()
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.DELETE_MESSAGE,
            message_id=None,
        )
        assert ok is False
        bot.delete_message.assert_not_awaited()

    async def test_ban_permanent_deletes_message_and_bans(self) -> None:
        bot = AsyncMock()
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.BAN_PERMANENT,
            message_id=100,
        )
        assert ok is True
        bot.delete_message.assert_awaited_once()
        bot.ban_chat_member.assert_awaited_once_with(chat_id=1, user_id=2)

    async def test_kick_bans_then_unbans(self) -> None:
        bot = AsyncMock()
        ok = await apply_punishment(
            bot=bot, chat_id=1, user_id=2, punishment=Punishment.KICK, message_id=None
        )
        assert ok is True
        bot.ban_chat_member.assert_awaited_once()
        bot.unban_chat_member.assert_awaited_once_with(
            chat_id=1, user_id=2, only_if_banned=True
        )

    async def test_mute_permanent_restricts_without_until_date(self) -> None:
        bot = AsyncMock()
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.MUTE_PERMANENT,
            message_id=None,
        )
        assert ok is True
        call_kwargs = bot.restrict_chat_member.call_args.kwargs
        assert "until_date" not in call_kwargs

    async def test_mute_24h_sets_until_date(self) -> None:
        bot = AsyncMock()
        ok = await apply_punishment(
            bot=bot, chat_id=1, user_id=2, punishment=Punishment.MUTE_24H, message_id=None
        )
        assert ok is True
        call_kwargs = bot.restrict_chat_member.call_args.kwargs
        assert "until_date" in call_kwargs

    async def test_forbidden_error_does_not_propagate(self) -> None:
        bot = AsyncMock()
        bot.delete_message.side_effect = TelegramForbiddenError(
            method=None, message="bot is not admin"
        )
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.DELETE_MESSAGE,
            message_id=100,
        )
        assert ok is False

    async def test_retry_after_error_does_not_propagate(self) -> None:
        bot = AsyncMock()
        bot.ban_chat_member.side_effect = TelegramRetryAfter(
            method=None, message="flood", retry_after=5
        )
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.BAN_PERMANENT,
            message_id=None,
        )
        assert ok is False

    async def test_unexpected_exception_does_not_propagate(self) -> None:
        """Критично: даже совершенно неожиданная ошибка не должна ронять бота."""
        bot = AsyncMock()
        bot.ban_chat_member.side_effect = RuntimeError("unexpected boom")
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.BAN_PERMANENT,
            message_id=None,
        )
        assert ok is False

    async def test_delete_failure_before_ban_does_not_block_ban(self) -> None:
        """Если удаление сообщения перед баном не удалось — бан всё равно применяется."""
        from aiogram.exceptions import TelegramBadRequest

        bot = AsyncMock()
        bot.delete_message.side_effect = TelegramBadRequest(
            method=None, message="message to delete not found"
        )
        ok = await apply_punishment(
            bot=bot,
            chat_id=1,
            user_id=2,
            punishment=Punishment.BAN_PERMANENT,
            message_id=100,
        )
        assert ok is True
        bot.ban_chat_member.assert_awaited_once()
