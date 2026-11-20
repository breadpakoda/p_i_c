"""
ai_engine/api/routes/layer3.py
──────────────────────────────
Dedicated routes for Layer-3 security alerts and standalone checks.

Exposes:
  POST /layer3-alert   – Receives alerts from bot.py and forwards to Node.js server
  POST /layer3-check   – Standalone Layer-3 scan utility endpoint
"""

import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from ai_engine.llm import conversation as conv_store
from ai_engine.llm.config import INTERNAL_SERVICE_TOKEN, NODE_ALERT_URL
from ai_engine.llm.persistence import (
    ConversationStatePersistenceError,
    save_conversation_state,
)

router = APIRouter(tags=["layer3"])
logger = logging.getLogger(__name__)


async def _persist_state(conversation_id: str, state: conv_store.ConversationState) -> None:
    try:
        await save_conversation_state(
            conversation_id,
            conv_store.serialize(state),
        )
    except ConversationStatePersistenceError as exc:
        logger.exception(
            "Unable to persist Layer 3 conversation state",
            extra={"conversation_id": conversation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "conversation_state_unavailable",
                "message": str(exc),
            },
        ) from exc


async def forward_prompt_injection_alert(
    conversation_id: str,
    layer1_score: float,
    layer2_score: float,
    combined_score: float,
) -> tuple[bool, int | None]:
    """Forward a detector alert without changing conversation security state."""
    if not NODE_ALERT_URL:
        logger.warning("Prompt-injection alert not forwarded: NODE_ALERT_URL is unset")
        return False, None

    payload = {
        "conversation_id": conversation_id,
        "is_malicious": True,
        "alert_type": "prompt_injection",
        "reason": "Layer 1 direct prompt-injection detection",
        "layer1_score": layer1_score,
        "layer2_score": layer2_score,
        "combined_score": combined_score,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {"x-internal-token": INTERNAL_SERVICE_TOKEN} if INTERNAL_SERVICE_TOKEN else {}
            response = await client.post(NODE_ALERT_URL, json=payload, headers=headers)
        if not response.is_success:
            logger.warning(
                "Prompt-injection alert forwarding failed with HTTP %s",
                response.status_code,
            )
        return response.is_success, response.status_code
    except httpx.HTTPError:
        logger.exception("Prompt-injection alert forwarding failed")
        return False, None


async def forward_layer3_security_alert(
    conversation_id: str,
    is_malicious: bool,
    reason: str,
) -> tuple[bool, int | None]:
    """Forward a Layer 3 decision to Node.js for authoritative persistence."""
    if not NODE_ALERT_URL:
        logger.error("Layer-3 alert not forwarded: NODE_ALERT_URL is unset")
        return False, None

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {"x-internal-token": INTERNAL_SERVICE_TOKEN} if INTERNAL_SERVICE_TOKEN else {}
            response = await client.post(
                NODE_ALERT_URL,
                json={
                    "conversation_id": conversation_id,
                    "is_malicious": is_malicious,
                    "reason": reason,
                },
                headers=headers,
            )
    except httpx.HTTPError:
        logger.exception(
            "Layer-3 alert forwarding failed",
            extra={"conversation_id": conversation_id},
        )
        return False, None

    return response.is_success, response.status_code


class Layer3AlertRequest(BaseModel):
    conversation_id: str
    is_malicious: bool
    messages: list[dict] = Field(default_factory=list)
    reason: str | None = None

    @field_validator("conversation_id")
    @classmethod
    def conversation_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("conversation_id must not be empty")
        return v.strip()


class Layer3AlertResponse(BaseModel):
    status: str
    conversation_id: str
    is_malicious: bool
    forwarded: bool
    node_response_status: int | None = None


class Layer3CheckRequest(BaseModel):
    conversation_id: str
    messages: list[dict]

    @field_validator("conversation_id")
    @classmethod
    def conversation_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("conversation_id must not be empty")
        return v.strip()


class Layer3CheckResponse(BaseModel):
    conversation_id: str
    is_danger: bool
    security_status: str


class Layer3ReviewRequest(BaseModel):
    conversation_id: str
    messages: list[dict]

    @field_validator("conversation_id")
    @classmethod
    def conversation_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("conversation_id must not be empty")
        return v.strip()


class Layer3ReviewResponse(BaseModel):
    conversation_id: str
    is_danger: bool


@router.post("/layer3-alert", response_model=Layer3AlertResponse)
async def layer3_alert(alert: Layer3AlertRequest) -> Layer3AlertResponse:
    """
    Receives Layer-3 security alerts.
    Updates the local conversation security status and forwards the alert
    to the configured Node.js server endpoint.
    """
    state = await conv_store.get_or_create(alert.conversation_id)
    async with state.lock:
        if alert.is_malicious:
            state.security_status = "blocked"
        await _persist_state(alert.conversation_id, state)

    forwarded, node_status_code = await forward_layer3_security_alert(
        alert.conversation_id,
        alert.is_malicious,
        alert.reason or "Layer 3 contextual prompt injection detected",
    )

    if alert.is_malicious and not forwarded:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "layer3_alert_forwarding_failed",
                "message": "The Node.js server did not confirm the Layer 3 security alert.",
            },
        )

    return Layer3AlertResponse(
        status="alert_processed",
        conversation_id=alert.conversation_id,
        is_malicious=alert.is_malicious,
        forwarded=forwarded,
        node_response_status=node_status_code,
    )


@router.post("/layer3-check", response_model=Layer3CheckResponse)
async def layer3_check(req: Layer3CheckRequest) -> Layer3CheckResponse:
    """
    Standalone Layer-3 security check utility endpoint.
    Runs is_danger_l3() on the provided messages list and updates security state.
    """
    from ai_engine.layer3 import is_danger_l3

    try:
        danger = await asyncio.to_thread(is_danger_l3, req.messages)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "layer3_invalid_response", "message": str(exc)},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "layer3_unavailable", "message": str(exc)},
        )

    state = await conv_store.get_or_create(req.conversation_id)
    async with state.lock:
        if danger:
            state.security_status = "blocked"
        elif state.security_status != "blocked":
            state.security_status = "clear"
        conv_store.mark_check_done(state)
        new_status = state.security_status
        await _persist_state(req.conversation_id, state)

    if danger:
        forwarded, _ = await forward_layer3_security_alert(
            req.conversation_id,
            True,
            "Layer 3 contextual prompt injection detected",
        )
        if not forwarded:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "layer3_alert_forwarding_failed",
                    "message": "The Node.js server did not confirm the Layer 3 security alert.",
                },
            )

    return Layer3CheckResponse(
        conversation_id=req.conversation_id,
        is_danger=danger,
        security_status=new_status,
    )


@router.post("/layer3-review", response_model=Layer3ReviewResponse)
async def layer3_review(
    req: Layer3ReviewRequest,
    request: Request,
) -> Layer3ReviewResponse:
    """Re-evaluate a legacy blocked transcript without changing current blocks."""
    client_host = request.client.host if request.client else ""
    authenticated_service = (
        INTERNAL_SERVICE_TOKEN
        and request.headers.get("x-internal-token") == INTERNAL_SERVICE_TOKEN
    )
    if (
        not authenticated_service
        and (
            client_host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
            or request.headers.get("origin")
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "internal_endpoint_only"},
        )

    if not req.messages or any(
        not isinstance(message, dict)
        or message.get("role") not in {"user", "assistant"}
        or not isinstance(message.get("content"), str)
        for message in req.messages
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_conversation_history"},
        )

    from ai_engine.layer3 import is_danger_l3

    try:
        danger = await asyncio.to_thread(is_danger_l3, req.messages)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "layer3_invalid_response", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "layer3_unavailable", "message": str(exc)},
        ) from exc

    state = await conv_store.get_or_create(req.conversation_id)
    async with state.lock:
        state.messages = [
            {"role": message["role"], "content": message["content"]}
            for message in req.messages
        ]
        state.message_count = len(state.messages)
        state.checked_up_to = state.message_count
        state.security_status = "blocked" if danger else "clear"
        await _persist_state(req.conversation_id, state)

    if danger:
        forwarded, _ = await forward_layer3_security_alert(
            req.conversation_id,
            True,
            "Layer 3 detected prompt injection in a reviewed conversation",
        )
        if not forwarded:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "layer3_alert_forwarding_failed",
                    "message": "The Node.js server did not confirm the Layer 3 security alert.",
                },
            )

    return Layer3ReviewResponse(
        conversation_id=req.conversation_id,
        is_danger=danger,
    )
