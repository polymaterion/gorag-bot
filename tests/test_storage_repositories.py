import pytest
from bot import ChatRepository
from bot import ChatSettingRepository
from bot import WhitelistedLinkRepository, WhitelistedLinkSenderRepository
from bot import BannedWordRepository
from bot import ScammerRepository
from bot import MessageHistoryRepository
from bot import JoinRequiredStateRepository

@pytest.mark.asyncio
async def test_chat_and_settings_roundtrip(db_session):
    chats = ChatRepository(db_session)
    chat = await chats.upsert(chat_id=100, title="Test", chat_type="supergroup")
    assert chat.id == 100

    settings = ChatSettingRepository(db_session)
    setting = await settings.get_or_create(100, "antispam.links", default_enabled=True)
    assert setting.enabled is True

    await settings.update_config(100, "antispam.links", {"window_seconds": 60})
    updated = await settings.get(100, "antispam.links")
    assert updated.config["window_seconds"] == 60

@pytest.mark.asyncio
async def test_whitelist_links(db_session):
    repo = WhitelistedLinkRepository(db_session)
    await repo.add(1, "example.com", added_by=999)
    items = await repo.list_for_chat(1)
    assert len(items) == 1
    removed = await repo.remove(1, "example.com")
    assert removed is True
    assert await repo.list_for_chat(1) == []

@pytest.mark.asyncio
async def test_scammer_global(db_session):
    repo = ScammerRepository(db_session)
    await repo.add(user_id=555, added_by=1, username="scammer1", reason="fraud")
    assert await repo.is_scammer(555) is True
    found = await repo.find_by_username("Scammer1")
    assert found is not None
    await repo.remove(555)
    assert await repo.is_scammer(555) is False

@pytest.mark.asyncio
async def test_message_history_duplicates(db_session):
    repo = MessageHistoryRepository(db_session)
    await repo.add(chat_id=1, user_id=2, message_id=10, text_normalized="hello world", topic_id=1)
    dups = await repo.find_recent_duplicates(1, "hello world", window_seconds=60, cross_topic=True)
    assert len(dups) == 1

@pytest.mark.asyncio
async def test_join_required_state(db_session):
    repo = JoinRequiredStateRepository(db_session)
    await repo.set_pending(chat_id=1, user_id=42)
    assert await repo.is_pending(1, 42) is True
    await repo.mark_satisfied(1, 42)
    assert await repo.is_pending(1, 42) is False
