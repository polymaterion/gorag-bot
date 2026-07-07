"""Тесты чекера повторяющихся сообщений."""
from __future__ import annotations

import pytest

from bot import DuplicateChecker, normalize_for_duplicate_match
from bot import MessageHistoryRepository


class TestNormalizeForDuplicateMatch:
    def test_case_and_punctuation_ignored(self) -> None:
        assert normalize_for_duplicate_match("Привет, мир!") == normalize_for_duplicate_match(
            "привет мир"
        )

    def test_extra_whitespace_collapsed(self) -> None:
        assert normalize_for_duplicate_match("  привет   мир  ") == "привет мир"

    def test_does_not_collapse_repeated_letters(self) -> None:
        # В отличие от antispam.words, здесь НЕ схлопываем повторяющиеся
        # буквы — иначе разные по интонации сообщения считались бы дублями.
        assert normalize_for_duplicate_match("приветттт") != normalize_for_duplicate_match(
            "привет"
        )

    def test_empty_text(self) -> None:
        assert normalize_for_duplicate_match("") == ""


@pytest.mark.asyncio
class TestDuplicateChecker:
    async def test_first_message_is_not_duplicate(self, db_session) -> None:
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        result = await checker.check(
            chat_id=1,
            user_id=10,
            message_id=100,
            text="hello world",
            topic_id=1,
            window_seconds=60,
            cross_topic=True,
        )
        assert result.is_duplicate is False

    async def test_exact_repeat_same_topic_is_duplicate(self, db_session) -> None:
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        await checker.check(
            chat_id=1, user_id=10, message_id=100, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        result = await checker.check(
            chat_id=1, user_id=10, message_id=101, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        assert result.is_duplicate is True
        assert 100 in result.matched_message_ids

    async def test_normalized_repeat_is_duplicate(self, db_session) -> None:
        """Совпадение после нормализации (разный регистр/пунктуация) тоже повтор."""
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        await checker.check(
            chat_id=1, user_id=10, message_id=100, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        result = await checker.check(
            chat_id=1, user_id=10, message_id=101, text="Hello, World!",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        assert result.is_duplicate is True

    async def test_cross_topic_duplicate_detected_when_enabled(self, db_session) -> None:
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        await checker.check(
            chat_id=1, user_id=10, message_id=100, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        result = await checker.check(
            chat_id=1, user_id=10, message_id=101, text="hello world",
            topic_id=2, window_seconds=60, cross_topic=True,
        )
        assert result.is_duplicate is True

    async def test_different_topic_not_duplicate_when_cross_topic_disabled(
        self, db_session
    ) -> None:
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        await checker.check(
            chat_id=1, user_id=10, message_id=100, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=False,
        )
        result = await checker.check(
            chat_id=1, user_id=10, message_id=101, text="hello world",
            topic_id=2, window_seconds=60, cross_topic=False,
        )
        assert result.is_duplicate is False

    async def test_different_text_not_duplicate(self, db_session) -> None:
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        await checker.check(
            chat_id=1, user_id=10, message_id=100, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        result = await checker.check(
            chat_id=1, user_id=10, message_id=101, text="completely different",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        assert result.is_duplicate is False

    async def test_different_chat_not_duplicate(self, db_session) -> None:
        """Повторы ищутся в рамках одного чата, не глобально."""
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        await checker.check(
            chat_id=1, user_id=10, message_id=100, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        result = await checker.check(
            chat_id=2, user_id=10, message_id=101, text="hello world",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        assert result.is_duplicate is False

    async def test_empty_text_never_duplicate(self, db_session) -> None:
        checker = DuplicateChecker(MessageHistoryRepository(db_session))
        result = await checker.check(
            chat_id=1, user_id=10, message_id=100, text="",
            topic_id=1, window_seconds=60, cross_topic=True,
        )
        assert result.is_duplicate is False
