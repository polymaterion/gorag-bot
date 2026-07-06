"""Тесты глобального списка скамеров."""
from __future__ import annotations

import pytest

from bot import parse_scammer_identifier
from bot import ScammerService
from bot import ScammerRepository


class TestParseScammerIdentifier:
    def test_numeric_user_id(self) -> None:
        assert parse_scammer_identifier("123456789") == (123456789, None)

    def test_at_username(self) -> None:
        assert parse_scammer_identifier("@scammer1") == (None, "scammer1")

    def test_bare_username(self) -> None:
        assert parse_scammer_identifier("scammer1") == (None, "scammer1")

    def test_profile_link_with_scheme(self) -> None:
        assert parse_scammer_identifier("https://t.me/scammer1") == (None, "scammer1")

    def test_profile_link_without_scheme(self) -> None:
        assert parse_scammer_identifier("t.me/scammer1") == (None, "scammer1")

    def test_empty_input(self) -> None:
        assert parse_scammer_identifier("") == (None, None)

    def test_whitespace_only(self) -> None:
        assert parse_scammer_identifier("   ") == (None, None)


@pytest.mark.asyncio
class TestScammerService:
    async def test_add_by_user_id(self, db_session) -> None:
        service = ScammerService(ScammerRepository(db_session))
        result = await service.add("555", added_by=1, reason="fraud")
        assert result.success is True
        assert result.scammer is not None
        assert result.scammer.user_id == 555

    async def test_add_without_recognizable_identifier_fails(self, db_session) -> None:
        service = ScammerService(ScammerRepository(db_session))
        result = await service.add("", added_by=1)
        assert result.success is False
        assert result.error is not None

    async def test_add_by_username_without_known_user_id_fails_first_time(
        self, db_session
    ) -> None:
        """
        Зафиксированное предположение: добавление ТОЛЬКО по username, без
        числового user_id, невозможно надёжно связать с аккаунтом через
        Bot API — сервис явно отказывает с понятным сообщением, вместо
        того чтобы создать запись с "фантомным" user_id.
        """
        service = ScammerService(ScammerRepository(db_session))
        result = await service.add("@unknown_user", added_by=1)
        assert result.success is False

    async def test_remove_by_user_id(self, db_session) -> None:
        repo = ScammerRepository(db_session)
        service = ScammerService(repo)
        await service.add("555", added_by=1)
        removed = await service.remove("555")
        assert removed is True
        assert await repo.is_scammer(555) is False

    async def test_remove_by_username_after_added_with_user_id(self, db_session) -> None:
        repo = ScammerRepository(db_session)
        service = ScammerService(repo)
        await service.add("555", added_by=1, reason="test")
        # username не был передан при добавлении по user_id в этом сценарии,
        # так что remove по username не найдёт совпадения — это ожидаемо.
        removed = await service.remove("@someone")
        assert removed is False

    async def test_list_all(self, db_session) -> None:
        service = ScammerService(ScammerRepository(db_session))
        await service.add("111", added_by=1)
        await service.add("222", added_by=1)
        all_scammers = await service.list_all()
        assert len(all_scammers) == 2

    async def test_remove_nonexistent_returns_false(self, db_session) -> None:
        service = ScammerService(ScammerRepository(db_session))
        removed = await service.remove("999999")
        assert removed is False
