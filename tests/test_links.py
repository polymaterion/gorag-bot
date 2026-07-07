"""Тесты чекера ссылок — чистые функции, БД не требуется."""
from __future__ import annotations

import pytest

from bot import DetectedLink, domain_matches_whitelist, find_links


@pytest.mark.parametrize(
    "text,expected_domains",
    [
        ("Зайди на https://example.com/page?x=1 срочно", {"example.com"}),
        ("www.example.com самый лучший сайт", {"example.com"}),
        ("example.com купи щас", {"example.com"}),
        ("example[.]com обход фильтра", {"example.com"}),
        ("example dot com классика", {"example.com"}),
        ("рaypal.com кириллическая а", {"paypal.com"}),
        (
            "два сайта: first.com и second.org одновременно",
            {"first.com", "second.org"},
        ),
    ],
)
def test_find_links_positive(text: str, expected_domains: set[str]) -> None:
    links = find_links(text)
    found_domains = {link.normalized_domain for link in links}
    assert found_domains == expected_domains


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Term. Ok. Fine.",
        "см. т.д. и т.п. дальше",
        "3.14 не ссылка",
        "Привет как дела? Хорошо, а у тебя.",
        "Ставка 1.5 на матч сегодня",
    ],
)
def test_find_links_no_false_positives(text: str) -> None:
    assert find_links(text) == []


def test_find_tme_link() -> None:
    links = find_links("подпишись t.me/somechannel пожалуйста")
    assert len(links) == 1
    assert links[0].kind == "tme"
    assert links[0].normalized_domain == "t.me/somechannel"


def test_find_tme_link_with_scheme() -> None:
    links = find_links("https://t.me/somechannel")
    assert len(links) == 1
    assert links[0].normalized_domain == "t.me/somechannel"


def test_no_duplicate_detection_for_same_span() -> None:
    # https:// ссылка не должна попадать в результаты дважды через
    # разные паттерны (url + bare_domain на том же фрагменте).
    links = find_links("https://example.com/some/path")
    assert len(links) == 1


class TestWhitelist:
    def test_exact_match(self) -> None:
        assert domain_matches_whitelist("example.com", ["example.com"]) is True

    def test_subdomain_covered(self) -> None:
        assert domain_matches_whitelist("sub.example.com", ["example.com"]) is True

    def test_unrelated_domain_not_covered(self) -> None:
        assert domain_matches_whitelist("evilexample.com", ["example.com"]) is False

    def test_case_insensitive(self) -> None:
        assert domain_matches_whitelist("EXAMPLE.com", ["example.COM"]) is True

    def test_empty_whitelist(self) -> None:
        assert domain_matches_whitelist("example.com", []) is False
