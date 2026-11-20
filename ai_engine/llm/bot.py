"""
ai_engine/llm/bot.py
─────────────────────
Target-LLM business logic.  No FastAPI code lives here.

Responsibilities
────────────────
1. Initialize the target LLM singleton once at import time.
2. Run the enabled Layer-1 security check on every incoming prompt.
3. Call the target LLM with the full conversation context.
4. Maintain conversation history via conversation.py.
5. Trigger Layer-3 checks after every N messages and post a security alert
   to the API layer if an injection is detected.

Public API
──────────
    generate_response(user_prompt, conversation_id) -> dict
"""

import asyncio

import httpx
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from .config import (
    LLM_BASE_URL,
    LLM_API_KEY,
    LLM_MODEL,
    L3_CHECK_THRESHOLD,
    L3_WINDOW_SIZE,
    API_ALERT_URL,
    INTERNAL_SERVICE_TOKEN,
)
from . import conversation as conv_store
from .persistence import save_conversation_state

LEGACY_COMBINED_THRESHOLD = 0.8
INJECTION_REFUSAL = (
    "A prompt injection attempt was detected. This request can't be fulfilled."
)
# ── Target LLM singleton (initialized once at import time) ───────────────────

_llm = ChatOpenAI(
    base_url=LLM_BASE_URL,
    api_key=LLM_API_KEY,
    model=LLM_MODEL,
    max_tokens=150,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_lc_messages(history: list[dict]) -> list:
    """
    Convert plain-dict conversation history into LangChain message objects
    so the full multi-turn context is sent to the target LLM.
    """
    lc_msgs = []
    for msg in history:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            lc_msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_msgs.append(AIMessage(content=content))
        else:
            lc_msgs.append(SystemMessage(content=content))
    return lc_msgs


def _run_layer1(prompt: str) -> dict:
    """
    Run Layer-1's LLM-based prompt-injection check on *prompt*.

    Returns:
        {"is_danger": bool, "attack_score": float}
    """
    from ai_engine.layer1 import is_danger
    result = is_danger(prompt)
    if isinstance(result, dict):
        flagged = bool(result.get("is_danger", False))
    else:
        flagged = bool(result)
    return {"is_danger": flagged, "attack_score": float(flagged)}


async def _post_l3_alert(conversation_id: str, is_malicious: bool, window: list[dict]) -> None:
    """
    POST a Layer-3 security alert to the API layer's /layer3-alert endpoint.

    The alert must be acknowledged by Node before the request completes.
    """
    payload = {
        "conversation_id": conversation_id,
        "is_malicious":    is_malicious,
        "messages":        window,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {"x-internal-token": INTERNAL_SERVICE_TOKEN} if INTERNAL_SERVICE_TOKEN else {}
            response = await client.post(API_ALERT_URL, json=payload, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Layer-3 alert delivery failed: {exc}") from exc

    try:
        forwarded = response.json().get("forwarded")
    except ValueError as exc:
        raise RuntimeError("Layer-3 alert endpoint returned invalid JSON.") from exc

    if forwarded is not True:
        raise RuntimeError("Layer-3 alert was not confirmed by the Node.js server.")


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_response(user_prompt: str, conversation_id: str) -> dict:
    """
    Process one user turn and return a structured response dict.

    Pipeline
    ────────
    1. Acquire the per-conversation lock and reject already-blocked conversations.
    2. Run Layer 1 on every prompt; refuse immediately when it returns True.
    3. Send allowed prompts and conversation history to the target LLM.
    4. Store both messages and run Layer 3 on each due 10-message batch.
    5. Persist state and forward a separate alert when Layer 3 detects an attack.

    Returns:
        {
            "conversation_id": str,
            "response":        str,
            "l1_flagged":      bool,
            "l2_flagged":      bool,
            "prompt_injection_detected": bool,
            "layer1_score":    float,
            "layer2_score":    float,
            "combined_score":  float,
            "combined_threshold": float,
            "l3_triggered":    bool,
            "l3_result":       bool | None,
        }

    Raises:
        RuntimeError – if the target LLM call fails or Layer-3 errors (fail-closed).
    """
    state = await conv_store.get_or_create(conversation_id)
    window: list[dict] = []
    answer = ""
    l1_flagged = False
    layer1_score = 0.0
    prompt_injection_detected = False
    l3_triggered = False
    l3_result: bool | None = None

    async with state.lock:
        if state.security_status == "blocked":
            raise RuntimeError(
                "This conversation has been blocked because a prompt injection attempt was detected."
            )

        try:
            l1 = await asyncio.to_thread(_run_layer1, user_prompt)
        except Exception as exc:
            raise RuntimeError(f"Layer-1 check failed: {exc}") from exc

        l1_flagged = bool(l1["is_danger"])
        prompt_injection_detected = l1_flagged
        layer1_score = float(l1_flagged)
        if l1_flagged:
            return {
                "conversation_id": conversation_id,
                "response": INJECTION_REFUSAL,
                "l1_flagged": True,
                "l2_flagged": False,
                "prompt_injection_detected": True,
                "layer1_score": layer1_score,
                "layer2_score": 0.0,
                "combined_score": layer1_score,
                "combined_threshold": LEGACY_COMBINED_THRESHOLD,
                "l3_triggered": False,
                "l3_result": None,
            }

        conv_store.add_message(state, "user", user_prompt)

        lc_messages = _build_lc_messages(state.messages)

        try:
            llm_response = _llm.invoke(lc_messages)
            answer = llm_response.content.strip()
        except Exception as exc:
            # Roll back user message so the counter stays consistent
            state.messages.pop()
            state.message_count -= 1
            raise RuntimeError(f"Target LLM call failed: {exc}") from exc

        conv_store.add_message(state, "assistant", answer)

        # ── Layer-3 trigger ───────────────────────────────────────────────
        l3_triggered = conv_store.should_check_l3(state, L3_CHECK_THRESHOLD)
        if l3_triggered:
            state.security_status = "pending"
            try:
                await save_conversation_state(
                    conversation_id,
                    conv_store.serialize(state),
                )
            except Exception:
                state.security_status = "blocked"
                conv_store.mark_check_done(state)
                raise

            window = conv_store.get_recent(state, L3_WINDOW_SIZE)

            try:
                from ai_engine.layer3 import is_danger_l3
                l3_result = await asyncio.to_thread(is_danger_l3, window)
            except Exception as exc:
                state.security_status = "blocked"
                conv_store.mark_check_done(state)
                await save_conversation_state(
                    conversation_id,
                    conv_store.serialize(state),
                )
                raise RuntimeError(
                    f"Layer-3 check failed (conversation blocked): {exc}"
                ) from exc

            if l3_result:
                state.security_status = "blocked"
            else:
                state.security_status = "clear"

            conv_store.mark_check_done(state)
            await save_conversation_state(
                conversation_id,
                conv_store.serialize(state),
            )
        else:
            await save_conversation_state(
                conversation_id,
                conv_store.serialize(state),
            )

    if l3_result:
        await _post_l3_alert(conversation_id, True, window)

    return {
        "conversation_id": conversation_id,
        "response":        answer,
        "l1_flagged":      l1_flagged,
        "l2_flagged":      False,
        "prompt_injection_detected": prompt_injection_detected,
        "layer1_score":    layer1_score,
        "layer2_score":    0.0,
        "combined_score":  layer1_score,
        "combined_threshold": LEGACY_COMBINED_THRESHOLD,
        "l3_triggered":    l3_triggered,
        "l3_result":       l3_result,
    }
