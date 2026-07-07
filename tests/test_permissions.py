"""Тесты проверки прав доступа (permissions) для admin-команд."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberAdministrator, ChatMemberMember, ChatMemberOwner, User

from bot import has_admin_access, is_chat_admin, is_global_admin


def _user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="Test")


def _owner(uid: int) -> ChatMemberOwner:
    return ChatMemberOwner(status="creator", user=_user(uid), is_anonymous=False)


def _member(uid: int) -> ChatMemberMember:
    return ChatMemberMember(status="member", user=_user(uid))


def _admin(uid: int, *, can_change_info: bool, can_promote_members: bool) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        status="administrator",
        user=_user(uid),
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=True,
        can_restrict_members=True,
        can_promote_members=can_promote_members,
        can_change_info=can_change_info,
        can_invite_users=True,
        can_pin_messages=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )


@pytest.mark.asyncio
class TestIsGlobalAdmin:
    async def test_configured_id_is_admin(self, monkeypatch) -> None:
        from bot import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("BOT_TOKEN", "test:token")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("GLOBAL_ADMIN_IDS", "111,222")
        assert await is_global_admin(111) is True
        assert await is_global_admin(333) is False
        get_settings.cache_clear()


@pytest.mark.asyncio
class TestIsChatAdmin:
    async def test_owner_has_access(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = _owner(1)
        assert await is_chat_admin(bot, chat_id=1, user_id=1) is True

    async def test_regular_member_denied(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = _member(2)
        assert await is_chat_admin(bot, chat_id=1, user_id=2) is False

    async def test_admin_with_change_info_right_has_access(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = _admin(
            5, can_change_info=True, can_promote_members=False
        )
        assert await is_chat_admin(bot, chat_id=1, user_id=5) is True

    async def test_admin_with_promote_right_has_access(self) -> None:
        bot = AsyncMock()
        bot.get_chat_member.return_value = _admin(
            6, can_change_info=False, can_promote_members=True
        )
        assert await is_chat_admin(bot, chat_id=1, user_id=6) is True

    async def test_content_only_admin_denied(self) -> None:
        """Админ без прав на управление чатом (только контент-модерация) — не имеет доступа."""
        bot = AsyncMock()
        bot.get_chat_member.return_value = _admin(
            7, can_change_info=False, can_promote_members=False
        )
        assert await is_chat_admin(bot, chat_id=1, user_id=7) is False

    async def test_api_error_fails_closed(self) -> None:
        """Критично: ошибка проверки прав НЕ должна давать доступ по умолчанию."""
        bot = AsyncMock()
        bot.get_chat_member.side_effect = TelegramAPIError(
            method=None, message="network error"
        )
        assert await is_chat_admin(bot, chat_id=1, user_id=3) is False


@pytest.mark.asyncio
class TestHasAdminAccess:
    async def test_global_admin_bypasses_live_chat_check(self, monkeypatch) -> None:
        from bot import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("BOT_TOKEN", "test:token")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("GLOBAL_ADMIN_IDS", "999")

        bot = AsyncMock()
        bot.get_chat_member.return_value = _member(999)  # даже "простой участник" в чате
        result = await has_admin_access(bot, chat_id=1, user_id=999)
        assert result is True
        bot.get_chat_member.assert_not_called()
        get_settings.cache_clear()

    async def test_non_global_admin_falls_through_to_chat_check(self, monkeypatch) -> None:
        from bot import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("BOT_TOKEN", "test:token")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("GLOBAL_ADMIN_IDS", "999")

        bot = AsyncMock()
        bot.get_chat_member.return_value = _owner(42)
        result = await has_admin_access(bot, chat_id=1, user_id=42)
        assert result is True
        bot.get_chat_member.assert_called_once()
        get_settings.cache_clear()
