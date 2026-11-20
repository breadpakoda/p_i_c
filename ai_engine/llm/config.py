"""
ai_engine/llm/config.py
───────────────────────
Centralised configuration for the LLM chatbot.

All tuneable values live here. Import from this module everywhere else so
that a single edit propagates through the whole package.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Target LLM credentials (from .env) ───────────────────────────────────────
LLM_BASE_URL: str = os.getenv("base_url_llm", "")
LLM_API_KEY:  str = os.getenv("api_llm", "")
LLM_MODEL:    str = os.getenv("model_llm", "")

# ── Layer-3 security check ────────────────────────────────────────────────────
# Layer 3 analyzes each completed batch of five turns (10 total messages).
L3_CHECK_THRESHOLD: int = 10
L3_WINDOW_SIZE: int = 10

# ── Security-alert forwarding ─────────────────────────────────────────────────
# URL of the FastAPI /layer3-alert endpoint (self) — used by bot.py to post
# Layer-3 alerts back through the API layer so Node.js can be notified.
# Override in .env if the API is running on a different host/port.
API_ALERT_URL: str = os.getenv("API_ALERT_URL", "http://localhost:8000/layer3-alert")

# URL of the Node.js security-alert endpoint. Configurable; not yet implemented.
NODE_ALERT_URL: str = os.getenv("NODE_ALERT_URL", "http://localhost:5000/security-alert")
INTERNAL_SERVICE_TOKEN: str = os.getenv("INTERNAL_SERVICE_TOKEN", "")

# Internal endpoint used to persist the AI-engine conversation state in MongoDB.
NODE_STATE_URL: str = os.getenv(
    "NODE_STATE_URL",
    "http://localhost:5000/internal/conversation-state",
)
