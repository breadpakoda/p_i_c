# AI Engine Detection Flow

## Active detection flow

Layer 1 checks every incoming prompt with its own ChatOpenAI instance and a
direct-injection-only security prompt. It uses the existing Layer 3 provider
settings (`base_url_l3`, `api_l3`, and `model_l3`) without sharing Layer 3's
client or system prompt. Only exact `TRUE` and `FALSE` verdicts are accepted.
`TRUE` refuses the prompt before it reaches the main target LLM; `FALSE` allows
normal response generation.

Layer 2 is disabled and is not imported or called by the chat pipeline. The
legacy response fields remain for compatibility: `layer2_score` is always
`0.0`, `l2_flagged` is always `false`, and `combined_score` mirrors the binary
Layer 1 result.

The target LLM uses its separate `base_url_llm`, `api_llm`, and `model_llm`
settings and only generates conversational responses. Layer 3 has its own LLM
client and contextual-analysis prompt. It checks the complete JSON history of
the latest 10 user and assistant messages after each batch, with a separate
message counter per conversation. Its check finishes before another prompt
can reach the target LLM.

If Layer 3 detects an indirect injection, the conversation is persisted as
blocked and a separate alert is sent through FastAPI to Node.js. The Node server
records the block in MongoDB and rejects subsequent requests. Failure to get a
Node.js acknowledgement is surfaced rather than silently treating the alert as
delivered. Layer 1 alerts are advisory and do not block the conversation.

The AI engine stores messages, the Layer 3 progress counter, and security
status through the Node API's loopback-only internal endpoint in MongoDB. On
restart, FastAPI restores the state; an interrupted `pending` Layer 3 check is
re-run before the conversation is allowed to continue. Node uses a
MongoDB-backed renewable lease to serialize requests for the same conversation
across multiple Node processes.
