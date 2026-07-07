"""Тесты загрузчика переводов."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bot import TranslationLoader


@pytest.fixture
def tmp_locales_dir():
    with tempfile.TemporaryDirectory() as tmp_dir:
        locales_path = Path(tmp_dir)
        (locales_path / "ru.yml").write_text(
            "common:\n  hello: \"Привет, {name}!\"\n  only_ru: \"Только в русском\"\n",
            encoding="utf-8",
        )
        (locales_path / "en.yml").write_text(
            "common:\n  hello: \"Hello, {name}!\"\n  only_en: \"Only in English\"\n",
            encoding="utf-8",
        )
        yield str(locales_path)


class TestTranslationLoader:
    def test_loads_available_locales(self, tmp_locales_dir: str) -> None:
        loader = TranslationLoader(tmp_locales_dir, fallback_locale="en")
        loader.load()
        assert set(loader.available_locales()) == {"ru", "en"}

    def test_get_translation_for_locale(self, tmp_locales_dir: str) -> None:
        loader = TranslationLoader(tmp_locales_dir, fallback_locale="en")
        result = loader.get("common.hello", "ru", name="Мир")
        assert result == "Привет, Мир!"

    def test_falls_back_to_fallback_locale_when_key_missing(
        self, tmp_locales_dir: str
    ) -> None:
        loader = TranslationLoader(tmp_locales_dir, fallback_locale="en")
        # only_en существует только в en.yml, но запрашиваем для ru — должен
        # найтись через fallback.
        result = loader.get("common.only_en", "ru")
        assert result == "Only in English"

    def test_falls_back_when_locale_itself_unknown(self, tmp_locales_dir: str) -> None:
        loader = TranslationLoader(tmp_locales_dir, fallback_locale="en")
        result = loader.get("common.hello", "fr", name="World")
        assert result == "Hello, World!"

    def test_missing_key_everywhere_returns_bracketed_key(
        self, tmp_locales_dir: str
    ) -> None:
        loader = TranslationLoader(tmp_locales_dir, fallback_locale="en")
        result = loader.get("nonexistent.key", "ru")
        assert result == "[nonexistent.key]"

    def test_lazy_loading_on_first_get(self, tmp_locales_dir: str) -> None:
        """load() не обязателен — первый get() должен сам загрузить переводы."""
        loader = TranslationLoader(tmp_locales_dir, fallback_locale="en")
        result = loader.get("common.hello", "ru", name="Тест")
        assert result == "Привет, Тест!"

    def test_missing_locales_dir_does_not_crash(self) -> None:
        loader = TranslationLoader("/nonexistent/path/xyz", fallback_locale="en")
        result = loader.get("common.hello", "ru")
        assert result == "[common.hello]"

    def test_adding_new_language_requires_no_code_change(self, tmp_locales_dir: str) -> None:
        """Раздел 9 критериев приёмки: новый язык = новый файл, без правки кода."""
        Path(tmp_locales_dir, "de.yml").write_text(
            "common:\n  hello: \"Hallo, {name}!\"\n", encoding="utf-8"
        )
        loader = TranslationLoader(tmp_locales_dir, fallback_locale="en")
        loader.load()
        assert "de" in loader.available_locales()
        assert loader.get("common.hello", "de", name="Welt") == "Hallo, Welt!"


@pytest.mark.asyncio
class TestLocaleMiddleware:
    """
    Тесты на устойчивость LocaleMiddleware к ошибкам БД. Это middleware
    выполняется для КАЖДОГО входящего апдейта до любого хэндлера — если оно
    падает при недоступной БД (обрыв соединения, не накатаны миграции,
    таблица chat_locale не существует), вся обработка апдейта (включая
    admin-команды) должна была бы упасть. Middleware обязан фолбэчиться на
    дефолтный язык и не пробрасывать исключение дальше.
    """

    async def test_db_error_does_not_propagate(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from contextlib import asynccontextmanager

        from bot import LocaleMiddleware, TranslationLoader

        @asynccontextmanager
        async def broken_session_scope():
            raise RuntimeError("relation \"chat_locale\" does not exist")
            yield  # pragma: no cover - недостижимо, нужно для синтаксиса generator

        monkeypatch.setattr("bot.session_scope", broken_session_scope)

        translator = TranslationLoader("locales", fallback_locale="en")
        translator.load()
        middleware = LocaleMiddleware(translator)

        event = MagicMock()
        event.chat = MagicMock(id=100)

        handler_called_with = {}

        async def fake_handler(event, data):
            handler_called_with.update(data)
            return "ok"

        result = await middleware(fake_handler, event, {})

        assert result == "ok"
        assert "locale" in handler_called_with
        assert handler_called_with["locale"] == "ru"  # default_locale по умолчанию

    async def test_successful_lookup_uses_chat_locale(self, monkeypatch, db_session) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock

        from bot import ChatLocaleRepository, LocaleMiddleware, TranslationLoader

        @asynccontextmanager
        async def fake_session_scope():
            yield db_session
            await db_session.commit()

        monkeypatch.setattr("bot.session_scope", fake_session_scope)

        await ChatLocaleRepository(db_session).set_locale(100, "en", fallback_code="en")

        translator = TranslationLoader("locales", fallback_locale="en")
        translator.load()
        middleware = LocaleMiddleware(translator)

        event = MagicMock()
        event.chat = MagicMock(id=100)

        handler_called_with = {}

        async def fake_handler(event, data):
            handler_called_with.update(data)
            return "ok"

        await middleware(fake_handler, event, {})
        assert handler_called_with["locale"] == "en"

    async def test_no_chat_id_uses_default_locale(self) -> None:
        from unittest.mock import MagicMock

        from bot import LocaleMiddleware, TranslationLoader

        translator = TranslationLoader("locales", fallback_locale="en")
        translator.load()
        middleware = LocaleMiddleware(translator)

        event = MagicMock(spec=[])  # без .chat и без .message вовсе

        handler_called_with = {}

        async def fake_handler(event, data):
            handler_called_with.update(data)
            return "ok"

        await middleware(fake_handler, event, {})
        assert handler_called_with["locale"] == "ru"
