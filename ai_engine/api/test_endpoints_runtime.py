from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("base_url_llm", "http://placeholder-llm")
os.environ.setdefault("api_llm", "placeholder-key")
os.environ.setdefault("model_llm", "placeholder-model")
os.environ.setdefault("base_url_l3", "http://placeholder-l3")
os.environ.setdefault("api_l3", "placeholder-key")
os.environ.setdefault("model_l3", "placeholder-model")

from ai_engine.api.app import app
from ai_engine.api.routes import layer3 as layer3_route
from ai_engine.llm import conversation as conv_store, persistence


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://test",
    ) as async_client:
        yield async_client


@pytest.fixture(autouse=True)
def clear_conversations():
    conv_store._store.clear()
    yield
    conv_store._store.clear()


def _llm_response(text: str) -> MagicMock:
    response = MagicMock()
    response.content = text
    return response


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "prompt-guard-api",
    }


@pytest.mark.asyncio
async def test_chat_then_status_persists_conversation(client: AsyncClient) -> None:
    target_llm = MagicMock()
    target_llm.invoke.return_value = _llm_response("Paris.")

    with patch(
        "ai_engine.llm.bot._run_layer1",
        return_value={"is_danger": False, "confidence": 0.01},
    ), patch("ai_engine.llm.bot._llm", target_llm):
        response = await client.post(
            "/chat",
            json={"prompt": "What is the capital of France?", "conversation_id": "persist-flow"},
        )

    assert response.status_code == 200
    assert response.json()["response"] == "Paris."

    status_response = await client.get("/status/persist-flow")
    assert status_response.status_code == 200
    assert status_response.json() == {
        "conversation_id": "persist-flow",
        "security_status": "clear",
        "message_count": 2,
    }
    target_llm.invoke.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "  ", "conversation_id": "invalid-prompt"},
        {"prompt": "hello", "conversation_id": "  "},
        {"prompt": "hello"},
    ],
)
async def test_chat_rejects_invalid_request(
    client: AsyncClient, payload: dict[str, str]
) -> None:
    response = await client.post("/chat", json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_status_returns_not_found(client: AsyncClient) -> None:
    response = await client.get("/status/not-created")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_status_restores_persisted_conversation_after_engine_restart(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_state(_conversation_id: str) -> dict:
        return {
            "messages": [
                {"role": "user", "content": "first turn"},
                {"role": "assistant", "content": "first reply"},
            ],
            "message_count": 2,
            "checked_up_to": 0,
            "security_status": "blocked",
        }

    monkeypatch.setattr(persistence, "load_conversation_state", load_state)
    response = await client.get("/status/persisted-after-restart")

    assert response.status_code == 200
    assert response.json()["security_status"] == "blocked"
    assert response.json()["message_count"] == 2
    assert conv_store.get_state("persisted-after-restart").messages[0]["content"] == "first turn"


@pytest.mark.asyncio
async def test_pending_status_is_rechecked_and_persisted(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_state(_conversation_id: str) -> dict:
        return {
            "messages": [
                {"role": "user", "content": "saved user message"},
                {"role": "assistant", "content": "saved response"},
            ],
            "message_count": 2,
            "checked_up_to": 0,
            "security_status": "pending",
        }

    save_state = AsyncMock()
    monkeypatch.setattr(persistence, "load_conversation_state", load_state)
    monkeypatch.setattr(persistence, "save_conversation_state", save_state)
    monkeypatch.setattr(
        "ai_engine.layer3.is_danger_l3",
        lambda _messages: False,
    )

    response = await client.get("/status/pending-after-restart")

    assert response.status_code == 200
    assert response.json()["security_status"] == "clear"
    save_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovered_malicious_pending_check_forwards_node_alert(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_state(_conversation_id: str) -> dict:
        return {
            "messages": [
                {"role": "user", "content": "first prompt"},
                {"role": "assistant", "content": "first response"},
            ],
            "message_count": 2,
            "checked_up_to": 0,
            "security_status": "pending",
        }

    save_state = AsyncMock()
    forward_alert = AsyncMock()
    monkeypatch.setattr(persistence, "load_conversation_state", load_state)
    monkeypatch.setattr(persistence, "save_conversation_state", save_state)
    monkeypatch.setattr(
        "ai_engine.layer3.is_danger_l3",
        lambda _messages: True,
    )
    monkeypatch.setattr("ai_engine.api.routes.chat._post_l3_alert", forward_alert)

    response = await client.get("/status/pending-attack-recovery")

    assert response.status_code == 200
    assert response.json()["security_status"] == "blocked"
    forward_alert.assert_awaited_once_with(
        "pending-attack-recovery",
        True,
        [
            {"role": "user", "content": "first prompt"},
            {"role": "assistant", "content": "first response"},
        ],
    )


@pytest.mark.asyncio
async def test_layer3_review_reclassifies_legacy_transcript_without_public_access(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_engine.layer3.is_danger_l3", lambda _messages: False)
    response = await client.post(
        "/layer3-review",
        json={
            "conversation_id": "legacy-review",
            "messages": [{"role": "user", "content": "ordinary historical request"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["is_danger"] is False
    state = conv_store.get_state("legacy-review")
    assert state.security_status == "clear"
    assert state.checked_up_to == 1


@pytest.mark.asyncio
async def test_layer3_review_rejects_browser_origin(client: AsyncClient) -> None:
    response = await client.post(
        "/layer3-review",
        headers={"origin": "https://untrusted.example"},
        json={
            "conversation_id": "forbidden-review",
            "messages": [{"role": "user", "content": "ordinary historical request"}],
        },
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_layer3_check_clears_and_blocks_conversations(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_engine.layer3 as layer3_package

    monkeypatch.setattr(
        layer3_route,
        "forward_layer3_security_alert",
        AsyncMock(return_value=(True, 202)),
    )
    verdicts = iter([False, True])
    monkeypatch.setattr(
        layer3_package, "is_danger_l3", lambda _messages: next(verdicts)
    )
    payload = {
        "conversation_id": "layer3-check-flow",
        "messages": [{"role": "user", "content": "test conversation"}],
    }

    safe_response = await client.post("/layer3-check", json=payload)
    assert safe_response.status_code == 200
    assert safe_response.json()["security_status"] == "clear"

    attack_response = await client.post("/layer3-check", json=payload)
    assert attack_response.status_code == 200
    assert attack_response.json()["is_danger"] is True
    assert attack_response.json()["security_status"] == "blocked"

    blocked_chat = await client.post(
        "/chat",
        json={"prompt": "follow-up", "conversation_id": "layer3-check-flow"},
    )
    assert blocked_chat.status_code == 403
    assert blocked_chat.json()["detail"]["error"] == "conversation_blocked"


@pytest.mark.asyncio
async def test_layer3_check_maps_invalid_model_response_to_gateway_error(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_engine.layer3 as layer3_package

    def invalid_verdict(_messages):
        raise ValueError("invalid verdict")

    monkeypatch.setattr(layer3_package, "is_danger_l3", invalid_verdict)
    response = await client.post(
        "/layer3-check",
        json={"conversation_id": "invalid-l3", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "layer3_invalid_response"


@pytest.mark.asyncio
async def test_layer3_alert_requires_node_confirmation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layer3_route, "NODE_ALERT_URL", "")
    response = await client.post(
        "/layer3-alert",
        json={
            "conversation_id": "layer3-alert-flow",
            "is_malicious": True,
            "messages": [{"role": "user", "content": "attack"}],
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "layer3_alert_forwarding_failed"
    assert conv_store.get_state("layer3-alert-flow").security_status == "blocked"


@pytest.mark.asyncio
async def test_layer3_alert_forwards_when_configured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layer3_route, "NODE_ALERT_URL", "http://node.test/security-alert")
    remote_response = SimpleNamespace(status_code=200, is_success=True)
    async_client = MagicMock()
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__.return_value = False
    async_client.post = AsyncMock(return_value=remote_response)

    with patch("ai_engine.api.routes.layer3.httpx.AsyncClient", return_value=async_client):
        response = await client.post(
            "/layer3-alert",
            json={"conversation_id": "forward-alert", "is_malicious": False},
        )

    assert response.status_code == 200
    assert response.json()["forwarded"] is True
    assert response.json()["node_response_status"] == 200
    async_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_layer1_alert_forwards_zero_layer2_score_without_changing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(layer3_route, "NODE_ALERT_URL", "http://node.test/security-alert")
    remote_response = SimpleNamespace(status_code=202, is_success=True)
    async_client = MagicMock()
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__.return_value = False
    async_client.post = AsyncMock(return_value=remote_response)

    with patch("ai_engine.api.routes.layer3.httpx.AsyncClient", return_value=async_client):
        forwarded, status_code = await layer3_route.forward_prompt_injection_alert(
            "layer1-alert-flow", 1.0, 0.0, 1.0
        )

    assert forwarded is True
    assert status_code == 202
    payload = async_client.post.await_args.kwargs["json"]
    assert payload["conversation_id"] == "layer1-alert-flow"
    assert payload["layer1_score"] == 1.0
    assert payload["layer2_score"] == 0.0
    assert payload["combined_score"] == 1.0
    assert payload["alert_type"] == "prompt_injection"
