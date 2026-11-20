"""
ai_engine/api/routes/chat.py
────────────────────────────
Chat routes for the FastAPI application.

Exposes:
  POST /chat
  GET  /status/{conversation_id}
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator

from ai_engine.llm import conversation as conv_store
from ai_engine.llm import persistence
from ai_engine.llm.bot import _post_l3_alert, generate_response
from ai_engine.llm.config import L3_WINDOW_SIZE
from ai_engine.api.routes.layer3 import forward_prompt_injection_alert

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    prompt: str
    conversation_id: str

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("prompt must not be empty")
        return v.strip()

    @field_validator("conversation_id")
    @classmethod
    def conversation_id_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("conversation_id must not be empty")
        return v.strip()


class ChatResponse(BaseModel):
    conversation_id: str
    response: str
    l1_flagged: bool = False
    l2_flagged: bool = False
    prompt_injection_detected: bool = False
    layer1_score: float = 0.0
    layer2_score: float = 0.0
    combined_score: float = 0.0
    combined_threshold: float = 0.8
    alert_forwarded: bool = False
    l3_triggered: bool = False
    l3_result: bool | None = None


class StatusResponse(BaseModel):
    conversation_id: str
    security_status: str
    message_count: int


async def _load_state(
    conversation_id: str,
) -> tuple[conv_store.ConversationState | None, bool]:
    state = conv_store.get_state(conversation_id)
    if state is not None:
        return state, False

    snapshot = await persistence.load_conversation_state(conversation_id)
    if snapshot is not None:
        return await conv_store.restore(conversation_id, snapshot), True
    return None, False


async def _resolve_pending_state(
    conversation_id: str,
    state: conv_store.ConversationState,
) -> None:
    window = []
    async with state.lock:
        if state.security_status != "pending":
            return

        try:
            from ai_engine.layer3 import is_danger_l3

            window = conv_store.get_recent(state, L3_WINDOW_SIZE)
            danger = await asyncio.to_thread(is_danger_l3, window)
        except Exception:
            logger.exception(
                "Pending Layer 3 recovery failed; conversation remains blocked",
                extra={"conversation_id": conversation_id},
            )
            state.security_status = "blocked"
            conv_store.mark_check_done(state)
            await persistence.save_conversation_state(
                conversation_id,
                conv_store.serialize(state),
            )
            return

        state.security_status = "blocked" if danger else "clear"
        conv_store.mark_check_done(state)
        await persistence.save_conversation_state(
            conversation_id,
            conv_store.serialize(state),
        )

    if danger:
        try:
            await _post_l3_alert(conversation_id, True, window)
        except RuntimeError as exc:
            logger.exception(
                "Recovered Layer 3 alert was not confirmed by Node.js",
                extra={"conversation_id": conversation_id},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "layer3_alert_forwarding_failed",
                    "message": str(exc),
                },
            ) from exc


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """
    Main chat route.
    1. Checks if the conversation is blocked or pending security analysis.
    2. Calls generate_response(), which runs Layer 1 before the target LLM.
    3. For a Layer 1 alert, forwards an advisory notification.
    4. Returns response JSON or a structured error.
    """
    try:
        existing, restored = await _load_state(req.conversation_id)
        if existing is not None and existing.security_status == "pending":
            if restored:
                await _resolve_pending_state(req.conversation_id, existing)
            else:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": "security_check_in_progress",
                        "message": (
                            "A security check is currently in progress for this "
                            "conversation. Please retry in a moment."
                        ),
                    },
                )
    except persistence.ConversationStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "conversation_state_unavailable",
                "message": str(exc),
            },
        ) from exc

    if existing is not None and existing.security_status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "conversation_blocked",
                "message": (
                    "This conversation has been blocked because a prompt "
                    "injection attempt was detected. Please start a new conversation."
                ),
            },
        )

    try:
        result = await generate_response(
            user_prompt=req.prompt,
            conversation_id=req.conversation_id,
        )
    except persistence.ConversationStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "conversation_state_unavailable",
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        err_msg = str(exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "message": err_msg},
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "blocked" in msg.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "conversation_blocked",
                    "message": msg,
                },
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "llm_error",
                "message": msg,
            },
        )

    if result["prompt_injection_detected"]:
        forwarded, _ = await forward_prompt_injection_alert(
            conversation_id=req.conversation_id,
            layer1_score=result["layer1_score"],
            layer2_score=result["layer2_score"],
            combined_score=result["combined_score"],
        )
        result["alert_forwarded"] = forwarded

    return ChatResponse(**result)


@router.get("/status/{conversation_id}", response_model=StatusResponse)
async def get_status(conversation_id: str) -> StatusResponse:
    """Return security status and total message count for a given conversation."""
    try:
        state, restored = await _load_state(conversation_id)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": f"Conversation '{conversation_id}' not found"},
            )
        if restored and state.security_status == "pending":
            await _resolve_pending_state(conversation_id, state)
    except persistence.ConversationStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "conversation_state_unavailable",
                "message": str(exc),
            },
        ) from exc

    return StatusResponse(
        conversation_id=conversation_id,
        security_status=state.security_status,
        message_count=state.message_count,
    )
