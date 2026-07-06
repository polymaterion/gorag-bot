"""Тесты точки входа приложения: регистрация модулей, dispatcher, graceful shutdown."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import ModuleRegistry
from bot import _build_dispatcher, _shutdown


class TestRegisterModules:
    def test_registers_all_three_core_modules(self, monkeypatch) -> None:
        test_registry = ModuleRegistry()
        monkeypatch.setattr("bot.registry", test_registry)

        from bot import _register_modules

        _register_modules()

        assert len(test_registry) == 3
        assert {m.name for m in test_registry.all()} == {
            "antispam",
            "scamlist",
            "join_required",
        }


class TestBuildDispatcher:
    def test_admin_router_included_before_moderation_router(self) -> None:
        dp = _build_dispatcher()
        sub_router_names = [r.name for r in dp.sub_routers]
        assert sub_router_names.index("admin_commands") < sub_router_names.index(
            "moderation_pipeline"
        )


@pytest.mark.asyncio
class TestShutdown:
    async def test_shutdown_sets_stop_event_and_closes_resources(self, monkeypatch) -> None:
        monkeypatch.setenv("SHUTDOWN_TIMEOUT_SECONDS", "1")
        from bot import get_settings

        get_settings.cache_clear()

        bot = AsyncMock()
        dp = MagicMock()
        dp.storage.close = AsyncMock()
        stop_event = asyncio.Event()

        async def cooperative_task():
            await stop_event.wait()

        tasks = [asyncio.create_task(cooperative_task(), name="t1")]

        await asyncio.wait_for(_shutdown(bot, dp, stop_event, tasks), timeout=3)

        assert stop_event.is_set()
        bot.session.close.assert_awaited_once()
        dp.storage.close.assert_awaited_once()
        assert tasks[0].done()
        assert not tasks[0].cancelled()
        get_settings.cache_clear()

    async def test_shutdown_force_cancels_unresponsive_tasks(self, monkeypatch) -> None:
        monkeypatch.setenv("SHUTDOWN_TIMEOUT_SECONDS", "1")
        from bot import get_settings

        get_settings.cache_clear()

        bot = AsyncMock()
        dp = MagicMock()
        dp.storage.close = AsyncMock()
        stop_event = asyncio.Event()

        async def unresponsive_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                pass

        tasks = [asyncio.create_task(unresponsive_task(), name="stuck")]

        await asyncio.wait_for(_shutdown(bot, dp, stop_event, tasks), timeout=5)

        # Задача сама поймала CancelledError через try/except и завершилась
        # штатно — done() истинно, а cancelled() ложно (это корректное
        # поведение asyncio для задач, обрабатывающих отмену явно).
        assert tasks[0].done()
        get_settings.cache_clear()

    async def test_shutdown_with_no_background_tasks(self, monkeypatch) -> None:
        monkeypatch.setenv("SHUTDOWN_TIMEOUT_SECONDS", "1")
        from bot import get_settings

        get_settings.cache_clear()

        bot = AsyncMock()
        dp = MagicMock()
        dp.storage.close = AsyncMock()
        stop_event = asyncio.Event()

        await asyncio.wait_for(_shutdown(bot, dp, stop_event, []), timeout=2)

        bot.session.close.assert_awaited_once()
        get_settings.cache_clear()
