"""Тесты чекера запрещённых слов — чистая функция, БД не требуется."""
from __future__ import annotations

import pytest

from bot import find_banned_word_match

BANNED = ["спам", "плохое слово", "казино"]


@pytest.mark.parametrize(
    "text,expected_phrase",
    [
        ("это спам сообщение", "спам"),
        ("СПАМ капсом тоже ловится", "спам"),
        ("СпАм вперемешку регистра", "спам"),
        ("это    плохое     слово с пробелами", "плохое слово"),
        ("плохое!!! слово с пунктуацией", "плохое слово"),
        ("го в казино играть", "казино"),
    ],
)
def test_find_banned_word_match_positive(text: str, expected_phrase: str) -> None:
    result = find_banned_word_match(text, BANNED)
    assert result is not None
    assert result.matched_phrase == expected_phrase


@pytest.mark.parametrize(
    "text",
    [
        "подкласс не должен матчиться со словом класс",
        "обычный чистый текст без нарушений",
        "",
    ],
)
def test_find_banned_word_match_negative(text: str) -> None:
    assert find_banned_word_match(text, BANNED) is None


def test_word_boundary_substring_not_matched() -> None:
    result = find_banned_word_match("подкласс не матчится", ["класс"])
    assert result is None


def test_word_boundary_standalone_word_matched() -> None:
    result = find_banned_word_match("это класс, отличный", ["класс"])
    assert result is not None


def test_empty_banned_list() -> None:
    assert find_banned_word_match("любой текст", []) is None


def test_empty_text() -> None:
    assert find_banned_word_match("", BANNED) is None


def test_repeated_char_obfuscation() -> None:
    # "ссссука" -> нормализация схлопывает 3+ повторов до одного символа
    result = find_banned_word_match("ссссссука сказал он", ["сука"])
    assert result is not None


def test_returns_first_match_original_phrase() -> None:
    """Возвращаемая фраза — оригинальная (не нормализованная) из списка."""
    result = find_banned_word_match("тут ПЛОХОЕ СЛОВО есть", ["плохое слово"])
    assert result is not None
    assert result.matched_phrase == "плохое слово"
