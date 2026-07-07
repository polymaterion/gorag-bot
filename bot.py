"""
Telegram Moderation Bot — модульный бот для модерации групп и супергрупп.

Всё в одном файле для простоты: конфигурация, модели БД, репозитории,
ядро (pipeline/registry), утилиты, i18n, модули модерации (антиспам,
скамлист, "пригласи друга"), наказания, admin-команды и точка входа.

Оглавление (секции):
  1.  Конфигурация (Settings)
  2.  ORM-модели (SQLAlchemy)
  3.  База данных: engine/session + репозитории
  4.  Наказания: enum + применение через Bot API
  5.  Ядро: типы событий, интерфейс модуля, реестр, pipeline
  6.  Утилиты: нормализация текста, парсер ссылок, TTL-очистка истории
  7.  i18n: загрузчик переводов + middleware
  8.  Модуль antispam (ссылки, слова, повторы)
  9.  Модуль scamlist (глобальный список скамеров)
  10. Модуль join_required ("пригласи друга")
  11. Admin: проверка прав + команды
  12. Модерационный router + точка входа (main)

Запуск: python bot.py
Требуется: BOT_TOKEN, DATABASE_URL (см. переменные окружения в самом низу
файла, у функции get_settings, и в README.md).
"""
from __future__ import annotations

import asyncio
import re
import signal
import unicodedata
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import structlog
import yaml
from aiogram import Bot, BaseMiddleware, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberOwner,
    ChatMemberUpdated,
    ChatPermissions,
    Message,
    TelegramObject,
)
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_engine_from_config,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

logger = structlog.get_logger(__name__)


# =============================================================================
# 1. Конфигурация (Settings)
# =============================================================================


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str = Field(..., alias="BOT_TOKEN")

    # Глобальные админы бота (список tg user_id), заданы через запятую:
    # GLOBAL_ADMIN_IDS=123456789,987654321
    # Храним как raw-строку: pydantic-settings по умолчанию пытается парсить
    # list[...] из env как JSON, что ломается на простом CSV-формате.
    global_admin_ids_raw: str = Field("", alias="GLOBAL_ADMIN_IDS")

    # --- База данных ---
    database_url: str = Field(..., alias="DATABASE_URL")

    # --- i18n ---
    default_locale: str = Field("ru", alias="DEFAULT_LOCALE")
    fallback_locale: str = Field("en", alias="FALLBACK_LOCALE")
    locales_dir: str = Field("locales", alias="LOCALES_DIR")

    # --- Хранилище истории сообщений (антидубликаты), TTL в секундах ---
    message_history_ttl_seconds: int = Field(86400, alias="MESSAGE_HISTORY_TTL_SECONDS")
    history_cleanup_interval_seconds: int = Field(
        3600, alias="HISTORY_CLEANUP_INTERVAL_SECONDS"
    )

    # --- Логирование ---
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # --- Прочее ---
    # Таймаут graceful shutdown (сек) — сколько ждём завершения текущих задач.
    shutdown_timeout_seconds: int = Field(15, alias="SHUTDOWN_TIMEOUT_SECONDS")

    @property
    def global_admin_ids(self) -> list[int]:
        raw = self.global_admin_ids_raw.strip()
        if not raw:
            return []
        return [int(part.strip()) for part in raw.split(",") if part.strip()]


@lru_cache
def get_settings() -> Settings:
    """Кешированный singleton настроек — читаем env один раз за процесс."""
    return Settings()  # type: ignore[call-arg]


# =============================================================================
# 2. ORM-модели (SQLAlchemy)
# =============================================================================

# В проде (Postgres) используем нативный JSONB, в тестах (SQLite) — обычный
# JSON. with_variant подменяет тип в зависимости от диалекта engine'а,
# поэтому модели остаются одинаковыми и для прода, и для unit-тестов.
JsonVariant = JSON().with_variant(JSONB(), "postgresql")

# Автоинкрементные суррогатные PK: BIGSERIAL на Postgres (реальный объём
# логов/истории сообщений может быть большим), но на SQLite (только для
# unit-тестов) autoincrement корректно работает лишь с "родным" Integer —
# отсюда variant. На бизнес-логику это не влияет, только на диалект в тестах.
BigIntegerPK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class Chat(Base):
    """Чат (группа/супергруппа), где установлен бот."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # tg_chat_id
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str] = mapped_column(String(32))  # group | supergroup
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    settings: Mapped[list["ChatSetting"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )
    locale: Mapped["ChatLocale | None"] = relationship(
        back_populates="chat", cascade="all, delete-orphan", uselist=False
    )


class ChatSetting(Base):
    """Настройки конкретного модуля для конкретного чата."""

    __tablename__ = "chat_settings"
    __table_args__ = (
        UniqueConstraint("chat_id", "module_name", name="uq_chat_module"),
    )

    id: Mapped[int] = mapped_column(BigIntegerPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    module_name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Наказание по умолчанию для правил этого модуля, если правило не
    # переопределяет своё собственное в config (см. apply_punishment).
    punishment: Mapped[str] = mapped_column(String(32), default="delete_message")
    config: Mapped[dict] = mapped_column(JsonVariant, default=dict, nullable=False)

    chat: Mapped["Chat"] = relationship(back_populates="settings")


class ChatAdmin(Base):
    """Доверенные пользователи чата (owner/admin), закешированные из Telegram API."""

    __tablename__ = "chat_admins"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(32))  # owner | admin
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WhitelistedLink(Base):
    """Белый список ссылок per-chat: домены или t.me каналы/группы."""

    __tablename__ = "whitelisted_links"
    __table_args__ = (
        UniqueConstraint("chat_id", "value", name="uq_chat_link_value"),
    )

    id: Mapped[int] = mapped_column(BigIntegerPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    value: Mapped[str] = mapped_column(String(255))  # домен или t.me/xxx
    added_by: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WhitelistedLinkSender(Base):
    """Пользователи, которым разрешено слать ссылки без ограничений."""

    __tablename__ = "whitelisted_link_senders"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_chat_link_sender"),
    )

    id: Mapped[int] = mapped_column(BigIntegerPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    added_by: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BannedWord(Base):
    """Запрещённые слова/фразы per-chat. Хранится нормализованная форма."""

    __tablename__ = "banned_words"
    __table_args__ = (
        UniqueConstraint("chat_id", "phrase_normalized", name="uq_chat_word"),
    )

    id: Mapped[int] = mapped_column(BigIntegerPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    phrase_normalized: Mapped[str] = mapped_column(String(255))
    phrase_original: Mapped[str] = mapped_column(String(255))
    added_by: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Scammer(Base):
    """
    Глобальный (не per-chat) список скамеров. Добавление возможно даже
    для пользователей, не состоящих ни в одном чате бота, поэтому
    первичный ключ — user_id (обязателен), username — опционален.
    """

    __tablename__ = "scammers"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    added_by: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MessageHistory(Base):
    """
    История сообщений для поиска повторов. TTL реализован не на уровне
    БД, а периодической job'ой (см. cleanup_message_history_once), которая
    удаляет строки старше TTL.
    """

    __tablename__ = "message_history"
    __table_args__ = (
        Index("ix_message_history_chat_created", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntegerPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    text_normalized: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModerationLog(Base):
    """Журнал действий модерации (аудит), отдельно от логов приложения."""

    __tablename__ = "moderation_log"
    __table_args__ = (
        Index("ix_moderation_log_chat_timestamp", "chat_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigIntegerPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatLocale(Base):
    """Языковые настройки чата."""

    __tablename__ = "chat_locale"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    locale_code: Mapped[str] = mapped_column(String(8), default="ru")
    fallback_code: Mapped[str] = mapped_column(String(8), default="en")

    chat: Mapped["Chat"] = relationship(back_populates="locale")


class JoinRequiredState(Base):
    """
    Состояние правила join_required per (chat, user): выполнил ли
    пользователь условие (пригласил кого-то) или всё ещё ограничен.
    """

    __tablename__ = "join_required_state"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|satisfied
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    satisfied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# =============================================================================
# 3. База данных: engine/session + репозитории
# =============================================================================

# ---------------------------------------------------------------------------
# 1. Engine / session factory
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,  # переживаем обрывы соединения (важно при переезде Railway->VPS)
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    Контекстный менеджер для repositories/сервисов: открывает сессию,
    коммитит при успехе, откатывает при исключении.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Вызывается при graceful shutdown, чтобы закрыть пул соединений."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


# ---------------------------------------------------------------------------
# 2. ChatRepository
# ---------------------------------------------------------------------------


class ChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, chat_id: int, title: str | None, chat_type: str) -> Chat:
        """
        Создаёт чат при первом появлении бота в нём, либо обновляет
        title/type/is_active, если чат уже существует (например, бота
        удалили и добавили заново, или чат переименовали).

        Реализовано через ORM get-or-create + мутацию, а не raw
        INSERT...ON CONFLICT DO UPDATE — см. подробный комментарий в
        JoinRequiredStateRepository.set_pending про identity-map staleness.
        """
        chat = await self._session.get(Chat, chat_id)
        if chat is None:
            chat = Chat(id=chat_id, title=title, type=chat_type, is_active=True)
            self._session.add(chat)
        else:
            chat.title = title
            chat.type = chat_type
            chat.is_active = True
        await self._session.flush()
        return chat

    async def get(self, chat_id: int) -> Chat | None:
        return await self._session.get(Chat, chat_id)

    async def mark_inactive(self, chat_id: int) -> None:
        """Бот был удалён/кикнут из чата — помечаем как неактивный (не удаляем данные)."""
        chat = await self._session.get(Chat, chat_id)
        if chat is not None:
            chat.is_active = False

    async def list_active(self) -> list[Chat]:
        result = await self._session.execute(select(Chat).where(Chat.is_active.is_(True)))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 3. ChatSettingRepository
# ---------------------------------------------------------------------------


class ChatSettingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, chat_id: int, module_name: str) -> ChatSetting | None:
        stmt = select(ChatSetting).where(
            ChatSetting.chat_id == chat_id, ChatSetting.module_name == module_name
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        chat_id: int,
        module_name: str,
        *,
        default_enabled: bool = False,
        default_config: dict | None = None,
        default_punishment: str = "delete_message",
    ) -> ChatSetting:
        existing = await self.get(chat_id, module_name)
        if existing is not None:
            return existing

        setting = ChatSetting(
            chat_id=chat_id,
            module_name=module_name,
            enabled=default_enabled,
            config=default_config or {},
            punishment=default_punishment,
        )
        self._session.add(setting)
        await self._session.flush()
        return setting

    async def set_enabled(self, chat_id: int, module_name: str, enabled: bool) -> None:
        setting = await self.get_or_create(chat_id, module_name)
        setting.enabled = enabled

    async def update_config(self, chat_id: int, module_name: str, config: dict) -> None:
        setting = await self.get_or_create(chat_id, module_name)
        # Мержим, а не затираем целиком — чтобы частичное обновление одного
        # параметра (например, "окно сравнения повторов") не сбрасывало
        # остальные настройки модуля.
        merged = {**setting.config, **config}
        setting.config = merged

    async def set_punishment(self, chat_id: int, module_name: str, punishment: str) -> None:
        setting = await self.get_or_create(chat_id, module_name)
        setting.punishment = punishment

    async def list_for_chat(self, chat_id: int) -> list[ChatSetting]:
        stmt = select(ChatSetting).where(ChatSetting.chat_id == chat_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 4. ChatAdminRepository
# ---------------------------------------------------------------------------


class ChatAdminRepository:
    """
    Локальный кеш прав администраторов чата (owner/admin), синхронизируется
    из Telegram Bot API (get_chat_administrators). Используется как
    быстрый путь для проверки прав в чекерах/командах; окончательное
    решение о правах на выполнение чувствительных действий всегда должно
    перепроверяться live через Bot API — см. admin.py::is_chat_admin.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_admins(self, chat_id: int, admins: list[tuple[int, str]]) -> None:
        """admins — список (user_id, role), role in {'owner', 'admin'}."""
        # Полная синхронизация: удаляем старый снапшот и вставляем новый,
        # чтобы разжалованные админы пропадали из кеша.
        await self._session.execute(delete(ChatAdmin).where(ChatAdmin.chat_id == chat_id))
        if admins:
            await self._session.execute(
                insert(ChatAdmin),
                [{"chat_id": chat_id, "user_id": uid, "role": role} for uid, role in admins],
            )

    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        stmt = select(ChatAdmin.user_id).where(
            ChatAdmin.chat_id == chat_id, ChatAdmin.user_id == user_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_for_chat(self, chat_id: int) -> list[ChatAdmin]:
        stmt = select(ChatAdmin).where(ChatAdmin.chat_id == chat_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 5. WhitelistedLinkRepository / WhitelistedLinkSenderRepository
# ---------------------------------------------------------------------------


class WhitelistedLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, chat_id: int, value: str, added_by: int) -> WhitelistedLink:
        """
        Select-then-insert-if-missing вместо raw ON CONFLICT DO NOTHING —
        портируемо между Postgres (прод) и SQLite (тесты), и не подвержено
        identity-map staleness (см. JoinRequiredStateRepository.set_pending).
        """
        existing = await self._session.execute(
            select(WhitelistedLink).where(
                WhitelistedLink.chat_id == chat_id, WhitelistedLink.value == value
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row

        link = WhitelistedLink(chat_id=chat_id, value=value, added_by=added_by)
        self._session.add(link)
        await self._session.flush()
        return link

    async def remove(self, chat_id: int, value: str) -> bool:
        stmt = delete(WhitelistedLink).where(
            WhitelistedLink.chat_id == chat_id, WhitelistedLink.value == value
        )
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def list_for_chat(self, chat_id: int) -> list[WhitelistedLink]:
        stmt = select(WhitelistedLink).where(WhitelistedLink.chat_id == chat_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class WhitelistedLinkSenderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, chat_id: int, user_id: int, added_by: int) -> WhitelistedLinkSender:
        existing = await self._session.execute(
            select(WhitelistedLinkSender).where(
                WhitelistedLinkSender.chat_id == chat_id,
                WhitelistedLinkSender.user_id == user_id,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row

        sender = WhitelistedLinkSender(chat_id=chat_id, user_id=user_id, added_by=added_by)
        self._session.add(sender)
        await self._session.flush()
        return sender

    async def remove(self, chat_id: int, user_id: int) -> bool:
        stmt = delete(WhitelistedLinkSender).where(
            WhitelistedLinkSender.chat_id == chat_id,
            WhitelistedLinkSender.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def is_whitelisted(self, chat_id: int, user_id: int) -> bool:
        stmt = select(WhitelistedLinkSender.id).where(
            WhitelistedLinkSender.chat_id == chat_id,
            WhitelistedLinkSender.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_for_chat(self, chat_id: int) -> list[WhitelistedLinkSender]:
        stmt = select(WhitelistedLinkSender).where(WhitelistedLinkSender.chat_id == chat_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 6. BannedWordRepository
# ---------------------------------------------------------------------------


class BannedWordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self, chat_id: int, phrase_normalized: str, phrase_original: str, added_by: int
    ) -> BannedWord:
        existing = await self._session.execute(
            select(BannedWord).where(
                BannedWord.chat_id == chat_id,
                BannedWord.phrase_normalized == phrase_normalized,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row

        word = BannedWord(
            chat_id=chat_id,
            phrase_normalized=phrase_normalized,
            phrase_original=phrase_original,
            added_by=added_by,
        )
        self._session.add(word)
        await self._session.flush()
        return word

    async def remove(self, chat_id: int, phrase_normalized: str) -> bool:
        stmt = delete(BannedWord).where(
            BannedWord.chat_id == chat_id,
            BannedWord.phrase_normalized == phrase_normalized,
        )
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def list_for_chat(self, chat_id: int) -> list[BannedWord]:
        stmt = select(BannedWord).where(BannedWord.chat_id == chat_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 7. ScammerRepository
# ---------------------------------------------------------------------------


class ScammerRepository:
    """
    Глобальный (не per-chat) список скамеров. Добавление не требует,
    чтобы пользователь состоял в каком-либо чате бота — user_id
    достаточно.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        user_id: int,
        added_by: int,
        username: str | None = None,
        reason: str | None = None,
    ) -> Scammer:
        scammer = await self._session.get(Scammer, user_id)
        if scammer is None:
            scammer = Scammer(
                user_id=user_id, username=username, added_by=added_by, reason=reason
            )
            self._session.add(scammer)
        else:
            scammer.username = username
            scammer.reason = reason
        await self._session.flush()
        return scammer

    async def remove(self, user_id: int) -> bool:
        stmt = delete(Scammer).where(Scammer.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.rowcount > 0

    async def is_scammer(self, user_id: int) -> bool:
        stmt = select(Scammer.user_id).where(Scammer.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def get(self, user_id: int) -> Scammer | None:
        return await self._session.get(Scammer, user_id)

    async def list_all(self) -> list[Scammer]:
        result = await self._session.execute(select(Scammer))
        return list(result.scalars().all())

    async def find_by_username(self, username: str) -> Scammer | None:
        normalized = username.lstrip("@").lower()
        stmt = select(Scammer).where(func.lower(Scammer.username) == normalized)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# 8. MessageHistoryRepository
# ---------------------------------------------------------------------------


class MessageHistoryRepository:
    """
    Хранилище истории сообщений для поиска повторов. Redis не используется —
    TTL реализован явной очисткой строк старше порога (см. utils.py::
    cleanup_message_history_once, вызывается по расписанию из bot.py).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        text_normalized: str,
        topic_id: int | None,
    ) -> MessageHistory:
        entry = MessageHistory(
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            text_normalized=text_normalized,
            topic_id=topic_id,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def find_recent_duplicates(
        self,
        chat_id: int,
        text_normalized: str,
        window_seconds: int,
        *,
        topic_id: int | None = None,
        cross_topic: bool = True,
        exclude_message_id: int | None = None,
    ) -> list[MessageHistory]:
        """
        Ищет сообщения с тем же нормализованным текстом в пределах
        временного окна. Если cross_topic=True — ищет по всему чату
        (между темами супергруппы), иначе только в пределах topic_id.
        """
        threshold = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        stmt = select(MessageHistory).where(
            MessageHistory.chat_id == chat_id,
            MessageHistory.text_normalized == text_normalized,
            MessageHistory.created_at >= threshold,
        )
        if not cross_topic:
            stmt = stmt.where(MessageHistory.topic_id == topic_id)
        if exclude_message_id is not None:
            stmt = stmt.where(MessageHistory.message_id != exclude_message_id)

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete_older_than(self, ttl_seconds: int) -> int:
        """Удаляет записи старше TTL. Возвращает количество удалённых строк."""
        threshold = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        stmt = delete(MessageHistory).where(MessageHistory.created_at < threshold)
        result = await self._session.execute(stmt)
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# 9. ModerationLogRepository
# ---------------------------------------------------------------------------


class ModerationLogRepository:
    """Аудит-лог модерации — отдельно от обычных логов приложения."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self, chat_id: int, user_id: int, event_type: str, reason: str
    ) -> ModerationLog:
        entry = ModerationLog(
            chat_id=chat_id, user_id=user_id, event_type=event_type, reason=reason
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def list_for_chat(self, chat_id: int, limit: int = 50) -> list[ModerationLog]:
        stmt = (
            select(ModerationLog)
            .where(ModerationLog.chat_id == chat_id)
            .order_by(ModerationLog.timestamp.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 10. ChatLocaleRepository
# ---------------------------------------------------------------------------


class ChatLocaleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, chat_id: int) -> ChatLocale | None:
        return await self._session.get(ChatLocale, chat_id)

    async def set_locale(
        self, chat_id: int, locale_code: str, fallback_code: str = "en"
    ) -> ChatLocale:
        locale = await self._session.get(ChatLocale, chat_id)
        if locale is None:
            locale = ChatLocale(
                chat_id=chat_id, locale_code=locale_code, fallback_code=fallback_code
            )
            self._session.add(locale)
        else:
            locale.locale_code = locale_code
            locale.fallback_code = fallback_code
        await self._session.flush()
        return locale


# ---------------------------------------------------------------------------
# 11. JoinRequiredStateRepository
# ---------------------------------------------------------------------------


class JoinRequiredStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, chat_id: int, user_id: int) -> JoinRequiredState | None:
        return await self._session.get(JoinRequiredState, (chat_id, user_id))

    async def set_pending(self, chat_id: int, user_id: int) -> JoinRequiredState:
        """
        Отмечает пользователя как обязанного пригласить кого-то.
        Используется при первом вступлении и при повторном входе после
        выхода (правило сбрасывается).

        Реализовано через ORM get-or-create + мутацию атрибутов, а НЕ через
        INSERT...ON CONFLICT DO UPDATE: последнее обходит identity map
        сессии — если объект уже был загружен в текущей сессии раньше,
        RETURNING из upsert возвращает актуальные данные, но уже загруженный
        Python-объект остаётся со старыми значениями до явного
        session.refresh(). Это давало трудноуловимый баг: проверка читала
        state.status как "satisfied", хотя в БД он уже был "pending" после
        rejoin. Get-or-create + мутация не подвержены этой проблеме.
        """
        state = await self.get(chat_id, user_id)
        now = datetime.now(timezone.utc)
        if state is None:
            state = JoinRequiredState(
                chat_id=chat_id, user_id=user_id, status="pending", joined_at=now
            )
            self._session.add(state)
        else:
            state.status = "pending"
            state.joined_at = now
            state.satisfied_at = None
        await self._session.flush()
        return state

    async def mark_satisfied(self, chat_id: int, user_id: int) -> None:
        state = await self.get(chat_id, user_id)
        if state is not None:
            state.status = "satisfied"
            state.satisfied_at = datetime.now(timezone.utc)

    async def is_pending(self, chat_id: int, user_id: int) -> bool:
        state = await self.get(chat_id, user_id)
        return state is not None and state.status == "pending"

    async def remove(self, chat_id: int, user_id: int) -> None:
        """При выходе из чата убираем состояние — при повторном входе будет set_pending заново."""
        state = await self.get(chat_id, user_id)
        if state is not None:
            await self._session.delete(state)


# =============================================================================
# 4. Наказания: enum + применение через Bot API
# =============================================================================

class Punishment(str, Enum):
    """Единый enum наказаний."""

    DELETE_MESSAGE = "delete_message"
    BAN_PERMANENT = "ban_permanent"
    MUTE_PERMANENT = "mute_permanent"
    MUTE_24H = "mute_24h"
    KICK = "kick"
    NONE = "none"  # нет наказания


# ---------------------------------------------------------------------------
# 2. Executor
# ---------------------------------------------------------------------------

MUTE_24H_SECONDS = 24 * 60 * 60


async def apply_punishment(
    *,
    bot: Bot,
    chat_id: int,
    user_id: int,
    punishment: Punishment,
    message_id: int | None,
) -> bool:
    """
    Применяет наказание через Telegram Bot API. Возвращает True при успехе,
    False при ошибке (ошибка уже залогирована). Все вызовы обёрнуты в
    try/except — ни одно исключение не должно ронять pipeline.

    message_id нужен только для DELETE_MESSAGE и предварительного удаления
    перед более серьёзными наказаниями; может быть None (например, для
    фоновой проверки join_required дедлайна, где нет конкретного сообщения).
    """
    try:
        if punishment == Punishment.NONE:
            return True

        if punishment == Punishment.DELETE_MESSAGE:
            if message_id is None:
                logger.warning(
                    "delete_message_without_message_id", chat_id=chat_id, user_id=user_id
                )
                return False
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True

        if punishment == Punishment.BAN_PERMANENT:
            if message_id is not None:
                await _safe_delete(bot, chat_id, message_id)
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            return True

        if punishment == Punishment.KICK:
            # Kick в Telegram Bot API = ban + немедленный unban.
            if message_id is not None:
                await _safe_delete(bot, chat_id, message_id)
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
            return True

        if punishment == Punishment.MUTE_PERMANENT:
            if message_id is not None:
                await _safe_delete(bot, chat_id, message_id)
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id, permissions=_MUTED_PERMISSIONS
            )
            return True

        if punishment == Punishment.MUTE_24H:
            if message_id is not None:
                await _safe_delete(bot, chat_id, message_id)
            until_date = datetime.now(timezone.utc) + timedelta(seconds=MUTE_24H_SECONDS)
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=_MUTED_PERMISSIONS,
                until_date=until_date,
            )
            return True

        logger.warning("unknown_punishment_type", punishment=str(punishment))
        return False

    except TelegramForbiddenError as exc:
        logger.warning(
            "punishment_forbidden",
            chat_id=chat_id,
            user_id=user_id,
            punishment=str(punishment),
            error=str(exc),
        )
        return False
    except TelegramRetryAfter as exc:
        logger.warning(
            "punishment_rate_limited",
            chat_id=chat_id,
            user_id=user_id,
            punishment=str(punishment),
            retry_after=exc.retry_after,
        )
        return False
    except TelegramAPIError as exc:
        logger.error(
            "punishment_telegram_api_error",
            chat_id=chat_id,
            user_id=user_id,
            punishment=str(punishment),
            error=str(exc),
        )
        return False
    except Exception as exc:  # noqa: BLE001 — последний рубеж
        logger.error(
            "punishment_unexpected_error",
            chat_id=chat_id,
            user_id=user_id,
            punishment=str(punishment),
            error=str(exc),
            exc_info=exc,
        )
        return False


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Удаление сообщения перед более серьёзным наказанием — ошибка здесь не критична."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as exc:
        logger.warning(
            "punishment_delete_message_failed",
            chat_id=chat_id,
            message_id=message_id,
            error=str(exc),
        )


def _build_muted_permissions():

    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


_MUTED_PERMISSIONS = _build_muted_permissions()


# =============================================================================
# 5. Ядро: типы событий, интерфейс модуля, реестр, pipeline
# =============================================================================

class EventType(str, Enum):
    """Типы событий, которые может обрабатывать модуль/чекер."""

    NEW_MESSAGE = "new_message"
    EDITED_MESSAGE = "edited_message"
    CHAT_MEMBER_JOINED = "chat_member_joined"
    CHAT_MEMBER_LEFT = "chat_member_left"
    NEW_CHAT_MEMBERS = "new_chat_members"  # кто-то добавил участника(ов)


@dataclass(slots=True)
class MessageContext:
    """
    Унифицированный контекст сообщения, передаваемый в pipeline. Поля
    намеренно плоские и примитивные — чтобы модули были чистыми функциями,
    тестируемыми без поднятия aiogram Bot/Dispatcher.
    """

    chat_id: int
    user_id: int
    message_id: int
    text: str | None
    topic_id: int | None  # message_thread_id для форум-супергрупп, иначе None
    created_at: datetime

    is_bot: bool = False
    is_chat_admin: bool = False
    is_global_admin: bool = False

    username: str | None = None
    full_name: str | None = None

    event_type: EventType = EventType.NEW_MESSAGE

    # Доп. данные, специфичные для конкретного события.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Violation:
    """Результат срабатывания чекера: найдено нарушение конкретного правила."""

    module_name: str
    rule_name: str
    punishment: Punishment
    reason: str  # человекочитаемая причина, для лога и уведомления
    i18n_key: str | None = None
    i18n_kwargs: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 2. ModerationModule
# ---------------------------------------------------------------------------


class ModerationModule(ABC):
    """
    Абстрактный базовый класс для модуля модерации. Каждый модуль
    (antispam, scamlist, join_required, ...) реализует этот протокол.
    Pipeline и registry работают только через него, не зная про
    конкретные модули.
    """

    name: str
    handled_events: list[EventType]
    default_config: dict[str, Any] = {}

    @abstractmethod
    async def is_enabled(self, chat_id: int) -> bool:
        """Включён ли модуль для данного чата."""
        raise NotImplementedError

    @abstractmethod
    async def check(self, ctx: MessageContext) -> Violation | None:
        """
        Основная проверка. Возвращает Violation при нарушении, иначе None.
        Не должна кидать исключения наружу — pipeline подстрахует
        try/except, но модуль обязан быть самодостаточным.
        """
        raise NotImplementedError

    async def on_error(self, exception: Exception, context: MessageContext) -> None:
        """Хук на случай ошибки внутри модуля. По умолчанию просто логирует."""
        module_logger = structlog.get_logger(module=self.name)
        module_logger.error(
            "module_check_error",
            chat_id=context.chat_id,
            user_id=context.user_id,
            error=str(exception),
            exc_info=exception,
        )


# ---------------------------------------------------------------------------
# 3. ModuleRegistry
# ---------------------------------------------------------------------------


class DuplicateModuleError(Exception):
    """Модуль с таким именем уже зарегистрирован."""


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, ModerationModule] = {}

    def register(self, module: ModerationModule) -> None:
        if module.name in self._modules:
            raise DuplicateModuleError(f"Модуль '{module.name}' уже зарегистрирован")
        self._modules[module.name] = module

    def get(self, name: str) -> ModerationModule | None:
        return self._modules.get(name)

    def all(self) -> list[ModerationModule]:
        return list(self._modules.values())

    def for_event(self, event_type: EventType) -> list[ModerationModule]:
        """Модули, подписанные на данный тип события."""
        return [m for m in self._modules.values() if event_type in m.handled_events]

    def __len__(self) -> int:
        return len(self._modules)

    def __contains__(self, name: str) -> bool:
        return name in self._modules


# Единственный экземпляр реестра на процесс. Заполняется в bot.py при
# старте бота; в тестах создаётся отдельный локальный экземпляр.
registry = ModuleRegistry()


# ---------------------------------------------------------------------------
# 4. PipelineRunner
# ---------------------------------------------------------------------------


class PipelineRunner:
    """
    Прогоняет MessageContext через все модули, подписанные на данный
    EventType (в порядке регистрации), и возвращает найденные нарушения.
    Каждый шаг обёрнут в try/except — упавший модуль не должен ронять
    обработку остальных модулей.
    """

    def __init__(self, module_registry: ModuleRegistry) -> None:
        self._registry = module_registry

    async def run(self, ctx: MessageContext, *, collect_all: bool = False) -> list[Violation]:
        """
        По умолчанию (collect_all=False) останавливается на первом
        найденном нарушении (early-exit), чтобы к одному сообщению не
        применялись сразу несколько наказаний.
        """
        violations: list[Violation] = []
        candidates = self._registry.for_event(ctx.event_type)

        for module in candidates:
            try:
                if not await module.is_enabled(ctx.chat_id):
                    continue
                violation = await module.check(ctx)
            except Exception as exc:  # noqa: BLE001 — намеренно широкий catch
                logger.error(
                    "pipeline_module_failed",
                    module=module.name,
                    chat_id=ctx.chat_id,
                    user_id=ctx.user_id,
                    error=str(exc),
                )
                try:
                    await module.on_error(exc, ctx)
                except Exception as hook_exc:  # noqa: BLE001
                    logger.error(
                        "pipeline_on_error_hook_failed",
                        module=module.name,
                        error=str(hook_exc),
                    )
                continue

            if violation is not None:
                violations.append(violation)
                if not collect_all:
                    break

        return violations


# =============================================================================
# 6. Утилиты: нормализация текста, парсер ссылок, TTL-очистка истории
# =============================================================================

_PUNCTUATION_RE = re.compile(r"[.,!?;:\"'`«»()\[\]{}—–_/\\|*#~^]")
_WHITESPACE_RE = re.compile(r"\s+")

# Таблица гомоглифов: латиница <-> кириллица визуально похожих символов.
# Нужна, чтобы "рaуpal" (кириллическая "р", "а" + латинские) нормализовалось
# так же, как "paypal", если это релевантно для слов/ссылок.
HOMOGLYPH_MAP = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "к": "k", "м": "m", "т": "t", "в": "b", "н": "h",
}


def normalize_text(text: str) -> str:
    """
    Нормализует текст для сравнения "по смыслу":
    - unicode-нормализация (NFKC — схлопывает визуально идентичные формы)
    - нижний регистр
    - гомоглифы кириллица->латиница (для распознавания обфускации)
    - схлопывание пробелов
    - удаление базовой пунктуации
    """
    if not text:
        return ""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.lower()
    normalized = "".join(HOMOGLYPH_MAP.get(ch, ch) for ch in normalized)
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def normalize_for_word_match(text: str) -> str:
    """
    То же самое, что normalize_text, но дополнительно схлопывает повторяющиеся
    символы внутри слов (ооочень -> очень) — частый способ обхода фильтра
    слов. Для поиска повторов сообщений это НЕ используется (там нужен
    более точный, менее "агрессивный" матч).
    """
    normalized = normalize_text(text)
    normalized = re.sub(r"(.)\1{2,}", r"\1", normalized)
    return normalized


def normalize_for_duplicate_match(text: str) -> str:
    """
    Нормализация для сравнения повторов: unicode NFKC, нижний регистр,
    схлопывание пробелов, удаление базовой пунктуации. Специально НЕ
    схлопывает повторяющиеся буквы (в отличие от normalize_for_word_match) —
    иначе "приветттт" и "привет" считались бы повтором, что для детекции
    дублей избыточно агрессивно.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


# ---------------------------------------------------------------------------
# 2. Парсер ссылок
# ---------------------------------------------------------------------------

# Список самых распространённых TLD + общий шаблон для 2-24 буквенных TLD.
_TLD_PATTERN = r"[a-zA-Zа-яА-Я]{2,24}"

_SCHEME_URL_RE = re.compile(r"(?P<url>https?://[^\s]+)", re.IGNORECASE)

_WWW_URL_RE = re.compile(
    r"(?P<url>www\.[a-zA-Z0-9][a-zA-Z0-9-]{0,61}\.%s(?:/[^\s]*)?)" % _TLD_PATTERN,
    re.IGNORECASE,
)

# "Голый" домен: буквы/цифры/дефис, точка, TLD. Требуем границы слова,
# чтобы не резать обычный текст.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w./])(?P<domain>[a-zA-Zа-яА-Я0-9](?:[a-zA-Zа-яА-Я0-9-]{0,61}[a-zA-Zа-яА-Я0-9])?"
    r"\.%s)(?![\w])" % _TLD_PATTERN
)

_TME_RE = re.compile(
    r"(?P<url>(?:https?://)?(?:t(?:elegram)?\.me)/(?P<path>[a-zA-Z0-9_+/]+))",
    re.IGNORECASE,
)

# Обфусцированные разделители домена: "example[.]com", "example (dot) com", "example,com".
_OBFUSCATED_DOT_RE = re.compile(
    r"(?P<domain>[a-zA-Zа-яА-Я0-9][a-zA-Zа-яА-Я0-9-]{0,61})"
    r"\s*(?:\[\.\]|\(\s*dot\s*\)|\s+dot\s+|,)\s*"
    r"(?P<tld>%s)\b" % _TLD_PATTERN,
    re.IGNORECASE,
)

# Частые "слова с точкой", которые не являются доменами (anti-false-positive).
_COMMON_FALSE_POSITIVE_TLDS = {"т.д", "т.п", "др"}


@dataclass(slots=True)
class DetectedLink:
    raw: str  # как ссылка встретилась в тексте
    normalized_domain: str  # нормализованный домен/путь для сверки с whitelist
    kind: str  # "url" | "www" | "bare_domain" | "tme" | "obfuscated"


def _deobfuscate_homoglyphs(text: str) -> str:
    return "".join(HOMOGLYPH_MAP.get(ch, ch) for ch in text.lower())


def _extract_domain_from_url(url: str) -> str:
    cleaned = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    cleaned = cleaned.split("/", 1)[0]
    cleaned = cleaned.split("?", 1)[0]
    return cleaned.lower().lstrip("www.")


def find_links(text: str) -> list[DetectedLink]:
    """
    Находит все ссылки (включая обфусцированные) в тексте: явные http(s)
    ссылки, www.domain.tld, "голые" домены, t.me/username, обфусцированные
    варианты ("example[.]com", "example dot com", гомоглифы).
    """
    if not text:
        return []

    results: list[DetectedLink] = []
    seen_spans: set[tuple[int, int]] = set()

    def _span_free(start: int, end: int) -> bool:
        return not any(s < end and start < e for s, e in seen_spans)

    for m in _TME_RE.finditer(text):
        if _span_free(m.start(), m.end()):
            seen_spans.add((m.start(), m.end()))
            path = m.group("path").lower()
            results.append(
                DetectedLink(raw=m.group("url"), normalized_domain=f"t.me/{path}", kind="tme")
            )

    for m in _SCHEME_URL_RE.finditer(text):
        if _span_free(m.start(), m.end()):
            seen_spans.add((m.start(), m.end()))
            domain = _extract_domain_from_url(m.group("url"))
            results.append(DetectedLink(raw=m.group("url"), normalized_domain=domain, kind="url"))

    for m in _WWW_URL_RE.finditer(text):
        if _span_free(m.start(), m.end()):
            seen_spans.add((m.start(), m.end()))
            domain = _extract_domain_from_url(m.group("url"))
            results.append(DetectedLink(raw=m.group("url"), normalized_domain=domain, kind="www"))

    # Голые домены, включая гомоглифную обфускацию (нормализуем сразу,
    # без отдельного повторного прохода, чтобы не задваивать результаты).
    for m in _BARE_DOMAIN_RE.finditer(text):
        if not _span_free(m.start(), m.end()):
            continue
        raw_domain = m.group("domain")
        domain = _deobfuscate_homoglyphs(raw_domain)
        if domain in _COMMON_FALSE_POSITIVE_TLDS:
            continue
        tld_part = domain.rsplit(".", 1)[-1]
        if tld_part.isdigit():
            continue
        seen_spans.add((m.start(), m.end()))
        kind = "obfuscated" if domain != raw_domain.lower() else "bare_domain"
        results.append(DetectedLink(raw=raw_domain, normalized_domain=domain, kind=kind))

    for m in _OBFUSCATED_DOT_RE.finditer(text):
        if not _span_free(m.start(), m.end()):
            continue
        seen_spans.add((m.start(), m.end()))
        domain = f"{m.group('domain').lower()}.{m.group('tld').lower()}"
        results.append(
            DetectedLink(raw=m.group(0), normalized_domain=domain, kind="obfuscated")
        )

    return results


def domain_matches_whitelist(domain: str, whitelist: list[str]) -> bool:
    """
    Проверяет домен против белого списка. Поддерживает поддомены:
    если в whitelist есть "example.com", то "sub.example.com" тоже
    считается разрешённым.
    """
    domain = domain.lower().lstrip("www.")
    for entry in whitelist:
        entry_normalized = entry.lower().lstrip("www.")
        if domain == entry_normalized or domain.endswith("." + entry_normalized):
            return True
    return False


# ---------------------------------------------------------------------------
# 3. Фоновая очистка message_history по TTL
# ---------------------------------------------------------------------------


async def cleanup_message_history_once() -> int:
    """Одна итерация очистки. Возвращает количество удалённых строк."""
    settings = get_settings()
    async with session_scope() as session:
        deleted = await MessageHistoryRepository(session).delete_older_than(
            settings.message_history_ttl_seconds
        )
    if deleted:
        logger.info("message_history_cleanup", deleted_rows=deleted)
    return deleted


async def run_message_history_cleanup_loop(stop_event: asyncio.Event) -> None:
    """
    Бесконечный цикл очистки, останавливаемый через stop_event (устанавливается
    при graceful shutdown в bot.py). Ошибки внутри одной итерации не
    останавливают цикл — логируются и ждём следующего интервала.
    """
    settings = get_settings()
    interval = settings.history_cleanup_interval_seconds

    while not stop_event.is_set():
        try:
            await cleanup_message_history_once()
        except Exception as exc:  # noqa: BLE001
            logger.error("message_history_cleanup_failed", error=str(exc))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue  # обычный путь — интервал истёк, продолжаем цикл


# =============================================================================
# 7. i18n: загрузчик переводов + middleware
# =============================================================================

class TranslationLoader:
    def __init__(self, locales_dir: str, fallback_locale: str = "en") -> None:
        self._locales_dir = Path(locales_dir)
        self._fallback_locale = fallback_locale
        self._translations: dict[str, dict[str, str]] = {}
        self._loaded = False

    def load(self) -> None:
        """
        Сканирует locales_dir на файлы *.yml, каждый файл — один язык
        (имя файла без расширения = код языка). Идемпотентно.
        """
        translations: dict[str, dict[str, str]] = {}

        if not self._locales_dir.exists():
            logger.warning("locales_dir_not_found", path=str(self._locales_dir))
            self._translations = translations
            self._loaded = True
            return

        for yml_file in sorted(self._locales_dir.glob("*.yml")):
            locale_code = yml_file.stem
            try:
                with yml_file.open("r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                flat = _flatten(data)
                translations[locale_code] = flat
            except Exception as exc:  # noqa: BLE001
                logger.error("locale_load_failed", file=str(yml_file), error=str(exc))

        self._translations = translations
        self._loaded = True
        logger.info("locales_loaded", locales=sorted(translations.keys()))

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def get(self, key: str, locale: str, fallback: str | None = None, **kwargs: object) -> str:
        """
        Возвращает перевод по ключу для указанного языка. Если не найден —
        пробует fallback. Если и там нет — возвращает "[key]" как явный
        индикатор отсутствия перевода.
        """
        self._ensure_loaded()
        fallback_locale = fallback or self._fallback_locale

        value = self._translations.get(locale, {}).get(key)
        if value is None:
            value = self._translations.get(fallback_locale, {}).get(key)
        if value is None:
            logger.warning("translation_missing", key=key, locale=locale)
            return f"[{key}]"

        if kwargs:
            try:
                return value.format(**kwargs)
            except (KeyError, IndexError) as exc:
                logger.error(
                    "translation_format_error", key=key, locale=locale, error=str(exc)
                )
                return value
        return value

    def available_locales(self) -> list[str]:
        self._ensure_loaded()
        return sorted(self._translations.keys())


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    """Превращает вложенный YAML (секции) в плоский dict с точечными ключами."""
    result: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(_flatten(value, full_key))
        else:
            result[full_key] = str(value)
    return result


_translator: TranslationLoader | None = None


def get_translator() -> TranslationLoader:
    """Singleton-инстанс, создаётся при первом использовании."""
    global _translator
    if _translator is None:

        settings = get_settings()
        _translator = TranslationLoader(
            locales_dir=settings.locales_dir, fallback_locale=settings.fallback_locale
        )
        _translator.load()
    return _translator


class LocaleMiddleware(BaseMiddleware):
    """
    Определяет язык текущего чата и прокидывает его (и сам TranslationLoader)
    в data хэндлеров под ключами "locale" и "t".
    """

    def __init__(self, translator: TranslationLoader | None = None) -> None:
        self._translator = translator or get_translator()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:

        chat_id = _extract_chat_id(event)
        locale = get_settings().default_locale

        if chat_id is not None:
            try:
                async with session_scope() as session:
                    chat_locale = await ChatLocaleRepository(session).get(chat_id)
                    if chat_locale is not None:
                        locale = chat_locale.locale_code
            except Exception as exc:  # noqa: BLE001
                # Middleware выполняется для КАЖДОГО апдейта до любого хэндлера —
                # если БД временно недоступна (обрыв соединения, не накатаны
                # миграции и т.п.), нельзя ронять обработку всего апдейта
                # (включая admin-команды) из-за того, что не удалось прочитать
                # locale. Молча используем дефолтный язык и логируем проблему.
                logger.error(
                    "locale_middleware_db_error", chat_id=chat_id, error=str(exc)
                )

        data["locale"] = locale
        data["t"] = self._translator
        return await handler(event, data)


def _extract_chat_id(event: TelegramObject) -> int | None:
    chat = getattr(event, "chat", None)
    if chat is not None:
        return chat.id
    message = getattr(event, "message", None)
    if message is not None and getattr(message, "chat", None) is not None:
        return message.chat.id
    return None


# =============================================================================
# 8. Модуль antispam (ссылки, слова, повторы)
# =============================================================================

@dataclass(slots=True)
class WordMatch:
    matched_phrase: str  # исходная (не нормализованная) фраза из списка запрещённых


def _phrase_to_pattern(normalized_phrase: str) -> re.Pattern[str]:
    """
    Строит regex с границами слов для нормализованной фразы, чтобы
    "класс" не матчился внутри "подкласс", но многословная фраза
    матчилась как последовательность слов с произвольным числом пробелов.
    """
    words = normalized_phrase.split(" ")
    escaped_words = [re.escape(w) for w in words if w]
    pattern = r"(?<!\w)" + r"\s+".join(escaped_words) + r"(?!\w)"
    return re.compile(pattern, re.UNICODE)


def find_banned_word_match(text: str, banned_phrases: list[str]) -> WordMatch | None:
    """
    Ищет первое совпадение с любой фразой из banned_phrases в тексте.
    banned_phrases — фразы в оригинальном виде, как их ввёл админ;
    нормализация происходит здесь же для сравнения.
    """
    if not text or not banned_phrases:
        return None

    normalized_text = normalize_for_word_match(text)
    if not normalized_text:
        return None

    for original_phrase in banned_phrases:
        normalized_phrase = normalize_for_word_match(original_phrase)
        if not normalized_phrase:
            continue
        pattern = _phrase_to_pattern(normalized_phrase)
        if pattern.search(normalized_text):
            return WordMatch(matched_phrase=original_phrase)

    return None


# ---------------------------------------------------------------------------
# 2. Проверка повторов
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DuplicateCheckResult:
    is_duplicate: bool
    matched_message_ids: list[int]


class DuplicateChecker:
    """
    Сервис поиска повторов. Каждый вызов check() одновременно:
    1) ищет существующие совпадения в истории за окно времени;
    2) сохраняет текущее сообщение в историю.
    Порядок важен: сначала ищем (чтобы не найти самого себя), потом пишем.
    """

    def __init__(self, history_repo: MessageHistoryRepository) -> None:
        self._history_repo = history_repo

    async def check(
        self,
        *,
        chat_id: int,
        user_id: int,
        message_id: int,
        text: str,
        topic_id: int | None,
        window_seconds: int,
        cross_topic: bool,
    ) -> DuplicateCheckResult:
        normalized = normalize_for_duplicate_match(text)

        if not normalized:
            return DuplicateCheckResult(is_duplicate=False, matched_message_ids=[])

        duplicates = await self._history_repo.find_recent_duplicates(
            chat_id=chat_id,
            text_normalized=normalized,
            window_seconds=window_seconds,
            topic_id=topic_id,
            cross_topic=cross_topic,
        )

        await self._history_repo.add(
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            text_normalized=normalized,
            topic_id=topic_id,
        )

        return DuplicateCheckResult(
            is_duplicate=len(duplicates) > 0,
            matched_message_ids=[d.message_id for d in duplicates],
        )


# ---------------------------------------------------------------------------
# 3. AntispamModule
# ---------------------------------------------------------------------------

ANTISPAM_DEFAULT_CONFIG: dict[str, Any] = {
    "links": {"enabled": True, "punishment": "delete_message"},
    "words": {"enabled": True, "punishment": "delete_message"},
    "duplicates": {
        "enabled": True,
        "punishment": "delete_message",
        "window_seconds": 60,
        "cross_topic": True,
    },
}


class AntispamModule(ModerationModule):
    """
    Объединяет links/words/duplicates под общий интерфейс ModerationModule.
    Каждый под-чекер имеет свой флаг enabled внутри config модуля; порядок
    проверки: links -> words -> duplicates, останов на первом нарушении.
    """

    name = "antispam"
    handled_events = [EventType.NEW_MESSAGE, EventType.EDITED_MESSAGE]
    default_config = ANTISPAM_DEFAULT_CONFIG

    async def is_enabled(self, chat_id: int) -> bool:
        async with session_scope() as session:
            setting = await ChatSettingRepository(session).get(chat_id, self.name)
            return setting.enabled if setting is not None else False

    async def check(self, ctx: MessageContext) -> Violation | None:
        if ctx.is_bot or ctx.is_chat_admin or ctx.is_global_admin:
            return None
        if not ctx.text:
            return None

        async with session_scope() as session:
            settings_repo = ChatSettingRepository(session)
            setting = await settings_repo.get_or_create(
                ctx.chat_id, self.name, default_config=ANTISPAM_DEFAULT_CONFIG
            )
            config = {**ANTISPAM_DEFAULT_CONFIG, **setting.config}

            links_cfg = {**ANTISPAM_DEFAULT_CONFIG["links"], **config.get("links", {})}
            words_cfg = {**ANTISPAM_DEFAULT_CONFIG["words"], **config.get("words", {})}
            dup_cfg = {**ANTISPAM_DEFAULT_CONFIG["duplicates"], **config.get("duplicates", {})}

            if links_cfg.get("enabled", True):
                violation = await self._check_links(ctx, session, links_cfg)
                if violation is not None:
                    return violation

            if words_cfg.get("enabled", True):
                violation = await self._check_words(ctx, session, words_cfg)
                if violation is not None:
                    return violation

            if dup_cfg.get("enabled", True):
                violation = await self._check_duplicates(ctx, session, dup_cfg)
                if violation is not None:
                    return violation

        return None

    async def _check_links(self, ctx: MessageContext, session, links_cfg: dict) -> Violation | None:
        detected = find_links(ctx.text or "")
        if not detected:
            return None

        sender_repo = WhitelistedLinkSenderRepository(session)
        if await sender_repo.is_whitelisted(ctx.chat_id, ctx.user_id):
            return None

        link_repo = WhitelistedLinkRepository(session)
        whitelist_entries = [w.value for w in await link_repo.list_for_chat(ctx.chat_id)]

        for link in detected:
            if domain_matches_whitelist(link.normalized_domain, whitelist_entries):
                continue
            return Violation(
                module_name=self.name,
                rule_name="links",
                punishment=Punishment(links_cfg.get("punishment", "delete_message")),
                reason=f"Обнаружена ссылка вне белого списка: {link.normalized_domain}",
                i18n_key="antispam.links.violation",
                i18n_kwargs={"domain": link.normalized_domain},
            )
        return None

    async def _check_words(self, ctx: MessageContext, session, words_cfg: dict) -> Violation | None:
        word_repo = BannedWordRepository(session)
        banned = [w.phrase_original for w in await word_repo.list_for_chat(ctx.chat_id)]
        if not banned:
            return None

        match = find_banned_word_match(ctx.text or "", banned)
        if match is None:
            return None

        return Violation(
            module_name=self.name,
            rule_name="words",
            punishment=Punishment(words_cfg.get("punishment", "delete_message")),
            reason=f"Обнаружено запрещённое слово/фраза: {match.matched_phrase}",
            i18n_key="antispam.words.violation",
            i18n_kwargs={"phrase": match.matched_phrase},
        )

    async def _check_duplicates(self, ctx: MessageContext, session, dup_cfg: dict) -> Violation | None:
        checker = DuplicateChecker(MessageHistoryRepository(session))
        result = await checker.check(
            chat_id=ctx.chat_id,
            user_id=ctx.user_id,
            message_id=ctx.message_id,
            text=ctx.text or "",
            topic_id=ctx.topic_id,
            window_seconds=int(dup_cfg.get("window_seconds", 60)),
            cross_topic=bool(dup_cfg.get("cross_topic", True)),
        )
        if not result.is_duplicate:
            return None

        return Violation(
            module_name=self.name,
            rule_name="duplicates",
            punishment=Punishment(dup_cfg.get("punishment", "delete_message")),
            reason="Обнаружено повторяющееся сообщение",
            i18n_key="antispam.duplicates.violation",
        )


# =============================================================================
# 9. Модуль scamlist (глобальный список скамеров)
# =============================================================================

SCAMLIST_DEFAULT_CONFIG = {"punishment": "ban_permanent"}

# Распознаём ссылку на профиль вида https://t.me/username или t.me/username
_PROFILE_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/(?P<username>[a-zA-Z0-9_]{5,32})", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# 1. parse_scammer_identifier
# ---------------------------------------------------------------------------


def parse_scammer_identifier(raw: str) -> tuple[int | None, str | None]:
    """
    Разбирает ввод админа при добавлении скамера: user_id (число),
    @username, или ссылка на профиль t.me/username.
    Возвращает (user_id, username) — один из них может быть None.
    """
    raw = raw.strip()
    if not raw:
        return None, None

    if raw.isdigit():
        return int(raw), None

    link_match = _PROFILE_LINK_RE.search(raw)
    if link_match:
        return None, link_match.group("username")

    if raw.startswith("@"):
        return None, raw[1:]

    if re.fullmatch(r"[a-zA-Z0-9_]{5,32}", raw):
        return None, raw

    return None, None


# ---------------------------------------------------------------------------
# 2. ScamlistModule
# ---------------------------------------------------------------------------


class ScamlistModule(ModerationModule):
    """
    Список скамеров общий для всего бота (не per-chat). is_enabled(chat_id)
    отвечает за флаг "применять ли в ЭТОМ чате автоматическое наказание",
    а не за существование самого списка (он существует всегда).
    """

    name = "scamlist"
    handled_events = [EventType.NEW_MESSAGE]
    default_config = SCAMLIST_DEFAULT_CONFIG

    async def is_enabled(self, chat_id: int) -> bool:
        async with session_scope() as session:
            setting = await ChatSettingRepository(session).get(chat_id, self.name)
            # По умолчанию включён — глобальный список скамеров должен
            # защищать чаты "из коробки", если админ явно не отключил.
            return setting.enabled if setting is not None else True

    async def check(self, ctx: MessageContext) -> Violation | None:
        if ctx.is_bot:
            return None

        async with session_scope() as session:
            scammer_repo = ScammerRepository(session)
            is_scammer = await scammer_repo.is_scammer(ctx.user_id)
            if not is_scammer:
                return None

            settings_repo = ChatSettingRepository(session)
            setting = await settings_repo.get_or_create(
                ctx.chat_id, self.name, default_config=SCAMLIST_DEFAULT_CONFIG, default_enabled=True
            )
            punishment_value = setting.config.get("punishment", SCAMLIST_DEFAULT_CONFIG["punishment"])

        return Violation(
            module_name=self.name,
            rule_name="global_scammer",
            punishment=Punishment(punishment_value),
            reason=f"Пользователь {ctx.user_id} состоит в глобальном списке скамеров",
            i18n_key="scamlist.violation",
        )


# ---------------------------------------------------------------------------
# 3. ScammerService
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AddScammerResult:
    success: bool
    scammer: Scammer | None = None
    error: str | None = None


class ScammerService:
    """CRUD-фасад над ScammerRepository с валидацией ввода, для admin-команд."""

    def __init__(self, repo: ScammerRepository) -> None:
        self._repo = repo

    async def add(
        self, identifier: str, added_by: int, reason: str | None = None
    ) -> AddScammerResult:
        user_id, username = parse_scammer_identifier(identifier)

        if user_id is None and username is None:
            return AddScammerResult(
                success=False,
                error="Не удалось распознать пользователя (нужен user_id, @username или ссылка на профиль)",
            )

        if user_id is None:
            existing = await self._repo.find_by_username(username)  # type: ignore[arg-type]
            if existing is not None:
                return AddScammerResult(success=True, scammer=existing)
            return AddScammerResult(
                success=False,
                error=(
                    "Пользователь известен только по username, а не по user_id. "
                    "Нужен числовой user_id или ссылка/пересланное сообщение от этого пользователя."
                ),
            )

        scammer = await self._repo.add(
            user_id=user_id, added_by=added_by, username=username, reason=reason
        )
        return AddScammerResult(success=True, scammer=scammer)

    async def remove(self, identifier: str) -> bool:
        user_id, username = parse_scammer_identifier(identifier)
        if user_id is not None:
            return await self._repo.remove(user_id)
        if username is not None:
            existing = await self._repo.find_by_username(username)
            if existing is not None:
                return await self._repo.remove(existing.user_id)
        return False

    async def list_all(self) -> list[Scammer]:
        return await self._repo.list_all()


# =============================================================================
# 10. Модуль join_required ("пригласи друга")
# =============================================================================

JOIN_REQUIRED_DEFAULT_CONFIG: dict[str, Any] = {
    "deadline_seconds": 3600,  # час на приглашение друга
    "punishment": "kick",
}

DEFAULT_CHECK_INTERVAL_SECONDS = 300  # проверяем дедлайны раз в 5 минут


# ---------------------------------------------------------------------------
# 1. JoinRequiredModule
# ---------------------------------------------------------------------------


class JoinRequiredModule(ModerationModule):
    """
    Событийно-управляемый модуль: реагирует на CHAT_MEMBER_JOINED (ставит
    pending), NEW_CHAT_MEMBERS (приглашающий выполнил условие), и NEW_MESSAGE
    как safety-net на случай, если фоновая проверка ещё не сработала.
    """

    name = "join_required"
    handled_events = [
        EventType.CHAT_MEMBER_JOINED,
        EventType.NEW_CHAT_MEMBERS,
        EventType.NEW_MESSAGE,
    ]
    default_config = JOIN_REQUIRED_DEFAULT_CONFIG

    async def is_enabled(self, chat_id: int) -> bool:
        async with session_scope() as session:
            setting = await ChatSettingRepository(session).get(chat_id, self.name)
            return setting.enabled if setting is not None else False

    async def check(self, ctx: MessageContext) -> Violation | None:
        async with session_scope() as session:
            settings_repo = ChatSettingRepository(session)
            setting = await settings_repo.get_or_create(
                ctx.chat_id, self.name, default_config=JOIN_REQUIRED_DEFAULT_CONFIG
            )
            config = {**JOIN_REQUIRED_DEFAULT_CONFIG, **setting.config}
            state_repo = JoinRequiredStateRepository(session)

            if ctx.event_type == EventType.CHAT_MEMBER_JOINED:
                await state_repo.set_pending(ctx.chat_id, ctx.user_id)
                return None

            if ctx.event_type == EventType.NEW_CHAT_MEMBERS:
                if await state_repo.is_pending(ctx.chat_id, ctx.user_id):
                    await state_repo.mark_satisfied(ctx.chat_id, ctx.user_id)
                return None

            if ctx.event_type == EventType.NEW_MESSAGE:
                state = await state_repo.get(ctx.chat_id, ctx.user_id)
                if state is None or state.status != "pending":
                    return None
                joined_at = state.joined_at
                if joined_at.tzinfo is None:
                    # SQLite (тесты) не сохраняет timezone-awareness даже для
                    # DateTime(timezone=True) — Postgres (прод) хранит корректно.
                    joined_at = joined_at.replace(tzinfo=timezone.utc)
                deadline = joined_at + timedelta(
                    seconds=int(config.get("deadline_seconds", 3600))
                )
                if datetime.now(timezone.utc) < deadline:
                    return None  # дедлайн ещё не прошёл

                return Violation(
                    module_name=self.name,
                    rule_name="deadline_exceeded",
                    punishment=Punishment(config.get("punishment", "kick")),
                    reason="Пользователь не пригласил друга в отведённое время",
                    i18n_key="join_required.violation",
                )

        return None


# ---------------------------------------------------------------------------
# 2. Фоновая проверка дедлайнов ("тихие" нарушители)
# ---------------------------------------------------------------------------


async def enforce_join_required_deadlines(bot: Bot) -> int:
    """
    Обходит все pending-записи, применяет наказание к тем, у кого истёк
    дедлайн, независимо от того, писали они что-то или нет. Наказание
    применяется напрямую через punishments.apply_punishment, а не через
    pipeline (здесь нет "события от пользователя" для MessageContext).
    """
    punished_count = 0

    async with session_scope() as session:
        settings_repo = ChatSettingRepository(session)
        stmt = select(JoinRequiredState).where(JoinRequiredState.status == "pending")
        result = await session.execute(stmt)
        pending_states = list(result.scalars().all())

        state_repo = JoinRequiredStateRepository(session)
        log_repo = ModerationLogRepository(session)

        for state in pending_states:
            setting = await settings_repo.get(state.chat_id, "join_required")
            if setting is None or not setting.enabled:
                continue

            deadline_seconds = int(setting.config.get("deadline_seconds", 3600))
            joined_at = state.joined_at
            if joined_at.tzinfo is None:
                joined_at = joined_at.replace(tzinfo=timezone.utc)
            deadline = joined_at + timedelta(seconds=deadline_seconds)
            if datetime.now(timezone.utc) < deadline:
                continue

            punishment = Punishment(setting.config.get("punishment", "kick"))

            try:
                await apply_punishment(
                    bot=bot,
                    chat_id=state.chat_id,
                    user_id=state.user_id,
                    punishment=punishment,
                    message_id=None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "join_required_enforce_failed",
                    chat_id=state.chat_id,
                    user_id=state.user_id,
                    error=str(exc),
                )
                continue

            await log_repo.record(
                chat_id=state.chat_id,
                user_id=state.user_id,
                event_type="join_required.deadline_exceeded",
                reason="Пользователь не пригласил друга в отведённое время (фоновая проверка)",
            )
            await state_repo.mark_satisfied(state.chat_id, state.user_id)
            punished_count += 1

    return punished_count


async def run_join_required_enforcer_loop(
    bot: Bot, stop_event: asyncio.Event, interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS
) -> None:
    """
    Периодический цикл, вызывающий enforce_join_required_deadlines через
    равные интервалы, останавливается через stop_event при graceful
    shutdown. Ошибки внутри одной итерации не останавливают цикл.
    """
    while not stop_event.is_set():
        try:
            count = await enforce_join_required_deadlines(bot)
            if count:
                logger.info("join_required_enforcer_tick", punished=count)
        except Exception as exc:  # noqa: BLE001
            logger.error("join_required_enforcer_failed", error=str(exc))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


# =============================================================================
# 11. Admin: проверка прав + команды
# =============================================================================

_ADMIN_STATUSES = ("creator", "administrator")


async def is_global_admin(user_id: int) -> bool:
    return user_id in get_settings().global_admin_ids


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """
    Живая проверка через Bot API: владелец или админ чата с правом
    управлять чатом. Если Bot API недоступен — по умолчанию ЗАПРЕЩАЕМ
    доступ (fail-closed), а не разрешаем.
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramAPIError as exc:
        logger.warning(
            "chat_admin_check_failed", chat_id=chat_id, user_id=user_id, error=str(exc)
        )
        return False

    if member.status not in _ADMIN_STATUSES:
        return False

    if isinstance(member, ChatMemberOwner):
        return True

    if isinstance(member, ChatMemberAdministrator):
        # Право "может менять настройки чата" — минимальное требование для
        # управления настройками модерации.
        return bool(member.can_change_info) or bool(member.can_promote_members)

    return False


async def has_admin_access(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Итоговая проверка: глобальный админ бота ИЛИ админ этого конкретного чата."""
    if await is_global_admin(user_id):
        return True
    return await is_chat_admin(bot, chat_id, user_id)


# ---------------------------------------------------------------------------
# 2. Admin-команды
# ---------------------------------------------------------------------------

admin_router = Router(name="admin_commands")

_VALID_PUNISHMENTS = {p.value for p in Punishment}
_ANTISPAM_SUBMODULES = {"links", "words", "duplicates"}


async def _require_admin_access(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> bool:
    """
    Общая проверка прав для команд. Возвращает True, если доступ разрешён.
    При отказе сама отправляет понятное сообщение пользователю.
    """
    if message.chat.type not in ("group", "supergroup"):
        await message.answer(t.get("common.chat_only", locale))
        return False

    if message.from_user is None:
        return False

    allowed = await has_admin_access(bot, message.chat.id, message.from_user.id)
    if not allowed:
        await message.answer(t.get("common.no_permission", locale))
        return False
    return True


# ---------------------------------------------------------------------------
# Сводка настроек
# ---------------------------------------------------------------------------


@admin_router.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return

    async with session_scope() as session:
        settings_repo = ChatSettingRepository(session)
        chat_settings = await settings_repo.list_for_chat(message.chat.id)

    if not chat_settings:
        lines = [t.get("admin.current_settings_title", locale) + ":", t.get("admin.empty_list", locale)]
    else:
        lines = [t.get("admin.current_settings_title", locale) + ":"]
        for setting in sorted(chat_settings, key=lambda s: s.module_name):
            status = t.get("common.enabled", locale) if setting.enabled else t.get(
                "common.disabled", locale
            )
            lines.append(f"• {setting.module_name}: {status} (наказание: {setting.punishment})")

    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Антиспам: модуль целиком и под-модули
# ---------------------------------------------------------------------------


async def _set_module_enabled(
    message: Message, bot: Bot, t: TranslationLoader, locale: str, module_name: str, enabled: bool
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    async with session_scope() as session:
        await ChatSettingRepository(session).set_enabled(message.chat.id, module_name, enabled)

    key = "admin.module_enabled" if enabled else "admin.module_disabled"
    await message.answer(t.get(key, locale, module=module_name))


@admin_router.message(Command("antispam_on"))
async def cmd_antispam_on(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    await _set_module_enabled(message, bot, t, locale, "antispam", True)


@admin_router.message(Command("antispam_off"))
async def cmd_antispam_off(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    await _set_module_enabled(message, bot, t, locale, "antispam", False)


async def _set_antispam_submodule_enabled(
    message: Message,
    bot: Bot,
    t: TranslationLoader,
    locale: str,
    submodule: str,
    enabled: bool,
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    async with session_scope() as session:
        settings_repo = ChatSettingRepository(session)
        setting = await settings_repo.get_or_create(message.chat.id, "antispam")
        config = dict(setting.config)
        submodule_cfg = dict(config.get(submodule, {}))
        submodule_cfg["enabled"] = enabled
        config[submodule] = submodule_cfg
        await settings_repo.update_config(message.chat.id, "antispam", config)

    key = "admin.module_enabled" if enabled else "admin.module_disabled"
    await message.answer(t.get(key, locale, module=f"antispam.{submodule}"))


@admin_router.message(Command("antispam_links_on"))
async def cmd_antispam_links_on(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    await _set_antispam_submodule_enabled(message, bot, t, locale, "links", True)


@admin_router.message(Command("antispam_links_off"))
async def cmd_antispam_links_off(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    await _set_antispam_submodule_enabled(message, bot, t, locale, "links", False)


@admin_router.message(Command("antispam_words_on"))
async def cmd_antispam_words_on(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    await _set_antispam_submodule_enabled(message, bot, t, locale, "words", True)


@admin_router.message(Command("antispam_words_off"))
async def cmd_antispam_words_off(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    await _set_antispam_submodule_enabled(message, bot, t, locale, "words", False)


@admin_router.message(Command("antispam_duplicates_on"))
async def cmd_antispam_duplicates_on(
    message: Message, bot: Bot, t: TranslationLoader, locale: str
) -> None:
    await _set_antispam_submodule_enabled(message, bot, t, locale, "duplicates", True)


@admin_router.message(Command("antispam_duplicates_off"))
async def cmd_antispam_duplicates_off(
    message: Message, bot: Bot, t: TranslationLoader, locale: str
) -> None:
    await _set_antispam_submodule_enabled(message, bot, t, locale, "duplicates", False)


@admin_router.message(Command("antispam_duplicates_cross_topic_on"))
async def cmd_antispam_duplicates_cross_topic_on(
    message: Message, bot: Bot, t: TranslationLoader, locale: str
) -> None:
    """Глобальные повторы по темам (cross-topic) — отдельный тумблер внутри duplicates."""
    if not await _require_admin_access(message, bot, t, locale):
        return
    async with session_scope() as session:
        settings_repo = ChatSettingRepository(session)
        setting = await settings_repo.get_or_create(message.chat.id, "antispam")
        config = dict(setting.config)
        dup_cfg = dict(config.get("duplicates", {}))
        dup_cfg["cross_topic"] = True
        config["duplicates"] = dup_cfg
        await settings_repo.update_config(message.chat.id, "antispam", config)
    await message.answer(t.get("common.saved", locale))


@admin_router.message(Command("antispam_duplicates_cross_topic_off"))
async def cmd_antispam_duplicates_cross_topic_off(
    message: Message, bot: Bot, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    async with session_scope() as session:
        settings_repo = ChatSettingRepository(session)
        setting = await settings_repo.get_or_create(message.chat.id, "antispam")
        config = dict(setting.config)
        dup_cfg = dict(config.get("duplicates", {}))
        dup_cfg["cross_topic"] = False
        config["duplicates"] = dup_cfg
        await settings_repo.update_config(message.chat.id, "antispam", config)
    await message.answer(t.get("common.saved", locale))


# ---------------------------------------------------------------------------
# Белый список ссылок
# ---------------------------------------------------------------------------


@admin_router.message(Command("antispam_links_add"))
async def cmd_links_add(
    message: Message, bot: Bot, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    if not command.args:
        await message.answer(t.get("admin.usage_add_link", locale))
        return

    value = command.args.strip().lower()
    async with session_scope() as session:
        await WhitelistedLinkRepository(session).add(
            message.chat.id, value, added_by=message.from_user.id
        )
    await message.answer(t.get("antispam.links.added_to_whitelist", locale, value=value))


@admin_router.message(Command("antispam_links_remove"))
async def cmd_links_remove(
    message: Message, bot: Bot, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    if not command.args:
        await message.answer(t.get("admin.usage_remove_link", locale))
        return

    value = command.args.strip().lower()
    async with session_scope() as session:
        removed = await WhitelistedLinkRepository(session).remove(message.chat.id, value)
    if removed:
        await message.answer(t.get("antispam.links.removed_from_whitelist", locale, value=value))
    else:
        await message.answer(t.get("common.not_found", locale))


@admin_router.message(Command("antispam_links_list"))
async def cmd_links_list(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    async with session_scope() as session:
        items = await WhitelistedLinkRepository(session).list_for_chat(message.chat.id)

    title = t.get("admin.whitelist_links_title", locale)
    if not items:
        await message.answer(f"{title}:\n{t.get('admin.empty_list', locale)}")
        return
    lines = [f"{title}:"] + [f"• {item.value}" for item in items]
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Белый список отправителей ссылок
# ---------------------------------------------------------------------------


@admin_router.message(Command("antispam_senders_add"))
async def cmd_senders_add(
    message: Message, bot: Bot, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return

    target_user_id = _resolve_target_user_id(message, command)
    if target_user_id is None:
        await message.answer(t.get("admin.usage_add_sender", locale))
        return

    async with session_scope() as session:
        await WhitelistedLinkSenderRepository(session).add(
            message.chat.id, target_user_id, added_by=message.from_user.id
        )
    await message.answer(t.get("antispam.links.sender_added", locale, user_id=target_user_id))


@admin_router.message(Command("antispam_senders_remove"))
async def cmd_senders_remove(
    message: Message, bot: Bot, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return

    target_user_id = _resolve_target_user_id(message, command)
    if target_user_id is None:
        await message.answer(t.get("admin.usage_add_sender", locale))
        return

    async with session_scope() as session:
        removed = await WhitelistedLinkSenderRepository(session).remove(
            message.chat.id, target_user_id
        )
    if removed:
        await message.answer(t.get("antispam.links.sender_removed", locale, user_id=target_user_id))
    else:
        await message.answer(t.get("common.not_found", locale))


@admin_router.message(Command("antispam_senders_list"))
async def cmd_senders_list(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    async with session_scope() as session:
        items = await WhitelistedLinkSenderRepository(session).list_for_chat(message.chat.id)

    title = t.get("admin.whitelist_senders_title", locale)
    if not items:
        await message.answer(f"{title}:\n{t.get('admin.empty_list', locale)}")
        return
    lines = [f"{title}:"] + [f"• {item.user_id}" for item in items]
    await message.answer("\n".join(lines))


def _resolve_target_user_id(message: Message, command: CommandObject) -> int | None:
    """Определяет целевого пользователя: из reply, либо из аргумента команды (user_id)."""
    if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        return message.reply_to_message.from_user.id
    if command.args and command.args.strip().isdigit():
        return int(command.args.strip())
    return None


# ---------------------------------------------------------------------------
# Запрещённые слова
# ---------------------------------------------------------------------------


@admin_router.message(Command("antispam_words_add"))
async def cmd_words_add(
    message: Message, bot: Bot, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    if not command.args:
        await message.answer(t.get("admin.usage_add_word", locale))
        return

    phrase = command.args.strip()
    normalized = normalize_for_word_match(phrase)
    async with session_scope() as session:
        await BannedWordRepository(session).add(
            message.chat.id, normalized, phrase, added_by=message.from_user.id
        )
    await message.answer(t.get("antispam.words.added", locale, phrase=phrase))


@admin_router.message(Command("antispam_words_remove"))
async def cmd_words_remove(
    message: Message, bot: Bot, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    if not command.args:
        await message.answer(t.get("admin.usage_remove_word", locale))
        return

    phrase = command.args.strip()
    normalized = normalize_for_word_match(phrase)
    async with session_scope() as session:
        removed = await BannedWordRepository(session).remove(message.chat.id, normalized)
    if removed:
        await message.answer(t.get("antispam.words.removed", locale, phrase=phrase))
    else:
        await message.answer(t.get("common.not_found", locale))


@admin_router.message(Command("antispam_words_list"))
async def cmd_words_list(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return
    async with session_scope() as session:
        items = await BannedWordRepository(session).list_for_chat(message.chat.id)

    title = t.get("admin.banned_words_title", locale)
    if not items:
        await message.answer(f"{title}:\n{t.get('admin.empty_list', locale)}")
        return
    lines = [f"{title}:"] + [f"• {item.phrase_original}" for item in items]
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# Глобальный список скамеров (только глобальный админ бота)
# ---------------------------------------------------------------------------


@admin_router.message(Command("scamlist_add"))
async def cmd_scamlist_add(
    message: Message, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if message.from_user is None or not await is_global_admin(message.from_user.id):
        await message.answer(t.get("scamlist.only_global_admin", locale))
        return
    if not command.args:
        await message.answer(t.get("admin.usage_scammer_add", locale))
        return

    identifier = command.args.strip()
    async with session_scope() as session:
        service = ScammerService(ScammerRepository(session))
        result = await service.add(identifier, added_by=message.from_user.id)

    if result.success:
        await message.answer(t.get("scamlist.added", locale, identifier=identifier))
    else:
        await message.answer(result.error or t.get("common.invalid_input", locale))


@admin_router.message(Command("scamlist_remove"))
async def cmd_scamlist_remove(
    message: Message, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if message.from_user is None or not await is_global_admin(message.from_user.id):
        await message.answer(t.get("scamlist.only_global_admin", locale))
        return
    if not command.args:
        await message.answer(t.get("admin.usage_scammer_add", locale))
        return

    identifier = command.args.strip()
    async with session_scope() as session:
        service = ScammerService(ScammerRepository(session))
        removed = await service.remove(identifier)

    if removed:
        await message.answer(t.get("scamlist.removed", locale, identifier=identifier))
    else:
        await message.answer(t.get("common.not_found", locale))


@admin_router.message(Command("scamlist_list"))
async def cmd_scamlist_list(
    message: Message, t: TranslationLoader, locale: str
) -> None:
    if message.from_user is None or not await is_global_admin(message.from_user.id):
        await message.answer(t.get("scamlist.only_global_admin", locale))
        return

    async with session_scope() as session:
        service = ScammerService(ScammerRepository(session))
        items = await service.list_all()

    title = t.get("admin.scammers_title", locale)
    if not items:
        await message.answer(f"{title}:\n{t.get('admin.empty_list', locale)}")
        return
    lines = [f"{title}:"]
    for item in items:
        label = f"{item.user_id}" + (f" (@{item.username})" if item.username else "")
        lines.append(f"• {label}")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# join_required
# ---------------------------------------------------------------------------


@admin_router.message(Command("join_required_on"))
async def cmd_join_required_on(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return

    # Раздел 2.3 ТЗ: работает только там, где обычные участники могут
    # добавлять новых — проверяем права чата перед включением.
    chat = await bot.get_chat(message.chat.id)
    permissions = getattr(chat, "permissions", None)
    can_invite = permissions is None or getattr(permissions, "can_invite_users", True)
    if not can_invite:
        await message.answer(t.get("join_required.enabled_requires_permission", locale))
        return

    async with session_scope() as session:
        await ChatSettingRepository(session).set_enabled(message.chat.id, "join_required", True)
    await message.answer(t.get("admin.module_enabled", locale, module="join_required"))


@admin_router.message(Command("join_required_off"))
async def cmd_join_required_off(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    await _set_module_enabled(message, bot, t, locale, "join_required", False)


# ---------------------------------------------------------------------------
# Наказания
# ---------------------------------------------------------------------------


@admin_router.message(Command("set_punishment"))
async def cmd_set_punishment(
    message: Message, bot: Bot, command: CommandObject, t: TranslationLoader, locale: str
) -> None:
    if not await _require_admin_access(message, bot, t, locale):
        return

    if not command.args:
        await message.answer(t.get("admin.usage_punishment", locale))
        return

    parts = command.args.split()
    if len(parts) != 3:
        await message.answer(t.get("admin.usage_punishment", locale))
        return

    module_name, rule_name, punishment_value = parts
    if punishment_value not in _VALID_PUNISHMENTS:
        await message.answer(
            t.get("common.invalid_input", locale)
            + f" (доступные значения: {', '.join(sorted(_VALID_PUNISHMENTS))})"
        )
        return

    async with session_scope() as session:
        settings_repo = ChatSettingRepository(session)
        if module_name == "antispam" and rule_name in _ANTISPAM_SUBMODULES:
            setting = await settings_repo.get_or_create(message.chat.id, "antispam")
            config = dict(setting.config)
            submodule_cfg = dict(config.get(rule_name, {}))
            submodule_cfg["punishment"] = punishment_value
            config[rule_name] = submodule_cfg
            await settings_repo.update_config(message.chat.id, "antispam", config)
        else:
            await settings_repo.set_punishment(message.chat.id, module_name, punishment_value)

    await message.answer(
        t.get("admin.punishment_set", locale, rule=f"{module_name}.{rule_name}", punishment=punishment_value)
    )


# =============================================================================
# 12. Модерационный router + точка входа (main)
# =============================================================================

moderation_router = Router(name="moderation_pipeline")

_pipeline_runner = PipelineRunner(registry)


async def _build_context(
    bot: Bot, message: Message, event_type: EventType = EventType.NEW_MESSAGE
) -> MessageContext | None:
    """Собирает MessageContext из aiogram Message. None для событий не от пользователей."""
    if message.from_user is None:
        return None

    user_id = message.from_user.id
    is_bot = message.from_user.is_bot

    async with session_scope() as session:
        admin_repo = ChatAdminRepository(session)
        is_chat_admin_cached = await admin_repo.is_admin(message.chat.id, user_id)
    is_global = await is_global_admin(user_id)

    return MessageContext(
        chat_id=message.chat.id,
        user_id=user_id,
        message_id=message.message_id,
        text=message.text or message.caption,
        topic_id=message.message_thread_id,
        created_at=message.date,
        is_bot=is_bot,
        is_chat_admin=is_chat_admin_cached,
        is_global_admin=is_global,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        event_type=event_type,
    )


async def _handle_violation_or_none(
    bot: Bot, ctx: MessageContext, t: TranslationLoader, locale: str
) -> None:
    violations = await _pipeline_runner.run(ctx)
    if not violations:
        return

    violation = violations[0]
    success = await apply_punishment(
        bot=bot,
        chat_id=ctx.chat_id,
        user_id=ctx.user_id,
        punishment=violation.punishment,
        message_id=ctx.message_id,
    )

    async with session_scope() as session:
        await ModerationLogRepository(session).record(
            chat_id=ctx.chat_id,
            user_id=ctx.user_id,
            event_type=f"{violation.module_name}.{violation.rule_name}",
            reason=violation.reason,
        )

    logger.info(
        "violation_handled",
        chat_id=ctx.chat_id,
        user_id=ctx.user_id,
        module=violation.module_name,
        rule=violation.rule_name,
        punishment=str(violation.punishment),
        punishment_applied=success,
    )


@moderation_router.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated, bot: Bot) -> None:
    """
    Отслеживает вступление/выход участников — нужно для join_required
    (сброс/установка pending статуса).
    """
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    joined = old_status in ("left", "kicked") and new_status in ("member", "restricted")
    left = old_status in ("member", "administrator", "creator") and new_status in (
        "left",
        "kicked",
    )

    user_id = event.new_chat_member.user.id
    is_bot = event.new_chat_member.user.is_bot

    if joined and not is_bot:
        ctx = MessageContext(
            chat_id=event.chat.id,
            user_id=user_id,
            message_id=0,
            text=None,
            topic_id=None,
            created_at=event.date,
            is_bot=is_bot,
            event_type=EventType.CHAT_MEMBER_JOINED,
        )
        try:
            await _pipeline_runner.run(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "chat_member_joined_handler_failed",
                chat_id=event.chat.id,
                user_id=user_id,
                error=str(exc),
            )

    if left:
        try:
            async with session_scope() as session:
                await JoinRequiredStateRepository(session).remove(event.chat.id, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "chat_member_left_handler_failed",
                chat_id=event.chat.id,
                user_id=user_id,
                error=str(exc),
            )


@moderation_router.message(lambda message: bool(message.new_chat_members))
async def on_new_chat_members(message: Message, bot: Bot) -> None:
    """
    Кто-то добавил новых участников в чат — отмечаем это для join_required
    (пригласивший выполнил условие). Зарегистрирован ДО общего on_message,
    чтобы этот более специфичный фильтр перехватывал такие сообщения первым.
    """
    if message.from_user is None:
        return

    ctx = MessageContext(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        message_id=message.message_id,
        text=None,
        topic_id=message.message_thread_id,
        created_at=message.date,
        is_bot=message.from_user.is_bot,
        event_type=EventType.NEW_CHAT_MEMBERS,
        extra={"new_members": [u.id for u in (message.new_chat_members or [])]},
    )
    try:
        await _pipeline_runner.run(ctx)
    except Exception as exc:  # noqa: BLE001
        logger.error("new_chat_members_handler_failed", chat_id=message.chat.id, error=str(exc))


@moderation_router.edited_message()
async def on_edited_message(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    """Отредактированные сообщения тоже проверяются (пользователь мог вставить спам постфактум)."""
    if message.chat.type not in ("group", "supergroup"):
        return

    try:
        ctx = await _build_context(bot, message, EventType.EDITED_MESSAGE)
        if ctx is None:
            return

        await _handle_violation_or_none(bot, ctx, t, locale)
    except Exception as exc:  # noqa: BLE001
        # _build_context тоже обращается к БД (ChatAdminRepository) — раньше
        # вызывался до try/except, см. аналогичный комментарий в on_message.
        logger.error("on_edited_message_handler_failed", chat_id=message.chat.id, error=str(exc), exc_info=exc)


@moderation_router.message()
async def on_message(message: Message, bot: Bot, t: TranslationLoader, locale: str) -> None:
    """
    Главный вход для обычных сообщений (catch-all). Регистрирует чат при
    первом появлении, затем прогоняет через pipeline модерации. Должен быть
    ПОСЛЕДНИМ хэндлером в этом router и подключаться в main() ПОСЛЕ
    admin_router — см. комментарий в начале секции.
    """
    if message.text and message.text.startswith("/"):
        return  # команды обрабатываются admin router-ом, сюда попадать не должны

    if message.chat.type not in ("group", "supergroup"):
        return  # модерация применяется только к группам/супергруппам

    try:
        async with session_scope() as session:
            await ChatRepository(session).upsert(
                message.chat.id, message.chat.title, message.chat.type
            )

        ctx = await _build_context(bot, message, EventType.NEW_MESSAGE)
        if ctx is None:
            return

        await _handle_violation_or_none(bot, ctx, t, locale)
    except Exception as exc:  # noqa: BLE001 — последний рубеж, сообщение не должно ронять бота
        # Раньше только _handle_violation_or_none был обёрнут в try/except,
        # а ChatRepository.upsert и _build_context (оба обращаются к БД)
        # вызывались ДО этого блока — временная недоступность БД (обрыв
        # соединения, не накатаны миграции) роняла обработку каждого
        # обычного сообщения в чате. Теперь весь хэндлер защищён целиком.
        logger.error(
            "on_message_handler_failed",
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else None,
            error=str(exc),
            exc_info=exc,
        )


# ---------------------------------------------------------------------------
# 2. Инициализация приложения
# ---------------------------------------------------------------------------


def _configure_logging(log_level: str) -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            __import__("logging").getLevelName(log_level)
        ),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
    )


def _register_modules() -> None:
    """
    Единственное место в проекте, где перечисляются конкретные модули
    модерации. Чтобы добавить новый модуль: реализовать ModerationModule
    в отдельном файле (по образцу antispam.py/scamlist.py/join_required.py)
    и зарегистрировать здесь одной строкой.
    """
    registry.register(AntispamModule())
    registry.register(ScamlistModule())
    registry.register(JoinRequiredModule())
    logger.info("modules_registered", modules=[m.name for m in registry.all()])


def _build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    translator = get_translator()
    locale_middleware = LocaleMiddleware(translator)
    dp.message.middleware(locale_middleware)
    dp.edited_message.middleware(locale_middleware)

    # ВАЖНО: admin_router подключается ПЕРВЫМ, чтобы команды (/settings,
    # /antispam_on, ...) обрабатывались им раньше, чем общий catch-all
    # pipeline в moderation_router (см. комментарий в начале секции 1).
    dp.include_router(admin_router)
    dp.include_router(moderation_router)

    return dp


async def _run_background_tasks(bot: Bot, stop_event: asyncio.Event) -> list[asyncio.Task]:
    tasks = [
        asyncio.create_task(
            run_message_history_cleanup_loop(stop_event), name="history_cleanup_loop"
        ),
        asyncio.create_task(
            run_join_required_enforcer_loop(bot, stop_event), name="join_required_enforcer_loop"
        ),
    ]
    return tasks


async def _shutdown(
    bot: Bot, dp: Dispatcher, stop_event: asyncio.Event, background_tasks: list[asyncio.Task]
) -> None:
    settings = get_settings()
    logger.info("shutdown_initiated")

    stop_event.set()

    if background_tasks:
        _, pending = await asyncio.wait(
            background_tasks, timeout=settings.shutdown_timeout_seconds
        )
        for task in pending:
            logger.warning("background_task_did_not_stop_in_time", task=task.get_name())
            task.cancel()
        if pending:
            # Дожидаемся фактического завершения отменённых задач — cancel()
            # только выставляет запрос на отмену, задача отменяется
            # асинхронно на следующей возможности event loop.
            await asyncio.gather(*pending, return_exceptions=True)

    await dp.storage.close()
    await bot.session.close()
    await dispose_engine()
    logger.info("shutdown_complete")


async def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level)

    logger.info("starting_bot")
    _register_modules()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = _build_dispatcher()

    stop_event = asyncio.Event()
    background_tasks = await _run_background_tasks(bot, stop_event)

    loop = asyncio.get_running_loop()
    shutdown_task_holder: dict[str, asyncio.Task] = {}

    def _signal_handler() -> None:
        if "task" not in shutdown_task_holder:
            shutdown_task_holder["task"] = asyncio.create_task(
                _shutdown(bot, dp, stop_event, background_tasks)
            )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler для SIGTERM — не критично для деплоя.
            logger.warning("signal_handler_not_supported", signal=str(sig))

    try:
        await dp.start_polling(bot)
    finally:
        if "task" not in shutdown_task_holder:
            await _shutdown(bot, dp, stop_event, background_tasks)
        else:
            await shutdown_task_holder["task"]


if __name__ == "__main__":
    asyncio.run(main())
