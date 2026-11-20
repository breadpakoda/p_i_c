import pytest


@pytest.fixture(autouse=True)
def isolate_conversation_persistence(monkeypatch):
    async def no_persisted_state(_conversation_id):
        return None

    async def save_in_memory(_conversation_id, _state):
        return None

    monkeypatch.setattr(
        "ai_engine.llm.persistence.load_conversation_state",
        no_persisted_state,
    )
    monkeypatch.setattr(
        "ai_engine.llm.persistence.save_conversation_state",
        save_in_memory,
    )
    monkeypatch.setattr(
        "ai_engine.llm.bot.save_conversation_state",
        save_in_memory,
    )
    monkeypatch.setattr(
        "ai_engine.api.routes.layer3.save_conversation_state",
        save_in_memory,
    )
