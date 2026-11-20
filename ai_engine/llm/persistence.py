"""Persistence bridge for AI-engine conversation state in the Node/Mongo store."""

from urllib.parse import quote

import httpx

from .config import INTERNAL_SERVICE_TOKEN, NODE_STATE_URL


class ConversationStatePersistenceError(RuntimeError):
    """Raised when durable conversation state cannot be read or written."""


async def load_conversation_state(conversation_id: str) -> dict | None:
    url = f"{NODE_STATE_URL}/{quote(conversation_id, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {"x-internal-token": INTERNAL_SERVICE_TOKEN} if INTERNAL_SERVICE_TOKEN else {}
            response = await client.get(url, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ConversationStatePersistenceError(
            "Unable to load persisted conversation state."
        ) from exc

    state = response.json().get("state")
    if not isinstance(state, dict):
        raise ConversationStatePersistenceError(
            "The server returned invalid persisted conversation state."
        )
    return state


async def save_conversation_state(conversation_id: str, state: dict) -> None:
    url = f"{NODE_STATE_URL}/{quote(conversation_id, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {"x-internal-token": INTERNAL_SERVICE_TOKEN} if INTERNAL_SERVICE_TOKEN else {}
            response = await client.put(url, json={"state": state}, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ConversationStatePersistenceError(
            "Unable to save conversation state."
        ) from exc
