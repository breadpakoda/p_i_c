# Runtime Validation Report

**Generated:** 2026-11-03T00:46:13+05:30
**Target:** `D:\Academics\5th sem\Minor project\PBL\prompt-guard`

## Summary

| Step | Status | Exit code | Details |
|---|---|---:|---|
| Startup | PASS | n/a | Local MongoDB, FastAPI, and Vite were reachable; Node API health returned 200 after starting it for validation. |
| Server integration tests | PASS | 0 | `npm test` in `server`: 10 passed, 0 failed, including two Node processes sharing a conversation. |
| AI engine tests | PASS | 0 | `python -m pytest -q`: 95 passed. This includes real Layer 1/2 checks for 20 benign and 4 representative injection prompts. |
| Client lint/build | PASS | 0 | `npm run lint` and `npm run build` in `client` passed. |
| Browser E2E | PASS | 0 | `npm run test:e2e` in `client`: 4 Playwright flows passed against the local services. |

**Overall: PASS for the exercised local flows.** The reported issues have regression coverage. See the limitations below before treating the detector as production-calibrated.

## Changes Validated

1. **Reduced observed benign-prompt false positives.** Layer 1/2 scores now use a conservative fused cutoff of `0.8` and Layer 2 evidence centered at `0.35`. Regression tests exercise the local detector artifacts on 20 ordinary prompts and 4 injection prompts; all 24 had the expected classification.
2. **Persisted AI-engine conversation state.** Messages, Layer 3 progress, and security status are saved in MongoDB through the Node API. FastAPI restores saved state, and rechecks a pending Layer 3 decision restored after restart. A pending check still active in the current process remains fail-closed.
3. **Serialized writes across Node processes.** A renewable MongoDB lease coordinates requests for the same conversation across separate Node instances.
4. **Added recovery for older blocked chats.** Conversations blocked without recorded provenance can be explicitly rechecked by Layer 3. New/current blocks cannot use this recovery path.
5. **Restricted internal routes.** Node state/alert routes and the FastAPI legacy-review route require loopback callers; browser-origin requests are rejected. The Node JSON request body is capped at 2 MB.
6. **Fixed the initial-load UI race.** The client prevents creating a new conversation until its initial conversation list has loaded.
7. **Simplified Windows startup.** `start.ps1` checks MongoDB, dependencies, environment configuration, and model assets before opening separate service windows. Its PowerShell syntax was checked; the script was not launched because the services were already running.

Implementation details are in [server/index.js](./server/index.js), [chat.py](./ai_engine/api/routes/chat.py), [layer3.py](./ai_engine/api/routes/layer3.py), [ensemble.py](./ai_engine/llm/ensemble.py), [App.jsx](./client/src/App.jsx), and [start.ps1](./start.ps1).

## Test Evidence

| Suite | Coverage | Result |
|---|---|---|
| `server/test/*.test.js` | Chat persistence and validation, security blocks, pending-state recovery, two-process locking, legacy blocked-chat recheck, and internal-origin protection. | 10 passed |
| `ai_engine/llm/test_benign_prompt_corpus.py` | Real local Layer 1 model and Layer 2 index: 20 benign prompts allowed; 4 representative injection prompts detected. | 24 passed |
| Python suite | API, detector, conversation, persistence, and regression tests. | 95 passed |
| `client/e2e/chat.spec.js` | Multi-turn persistence/reload, refusal with safe follow-up, blocked-chat UI/server enforcement, and legacy recheck UI. | 4 passed |
| Client lint/build | ESLint and Vite production build. | Passed |

Commands:

```powershell
python -m pytest -q
Set-Location .\server
npm test
Set-Location ..\client
npm run lint
npm run build
npm run test:e2e
```

Browser E2E and server integration tests require local MongoDB, FastAPI, and Node services. E2E also requires the Playwright Chromium browser. The legacy-recheck UI test stubs its button response; the actual Node-to-FastAPI recheck path is covered by the server integration test.

## Exercised Browser Flows

- Sent multiple ordinary prompts and verified message history after refresh.
- Sent a representative injection prompt, verified refusal, then sent a benign follow-up successfully.
- Blocked a conversation through Layer 3 and verified disabled controls plus server-side 403 enforcement.
- Verified that an older blocked conversation exposes the Layer 3 recheck action and becomes usable after a safe result.

## Known Limitations

- The benign/injection corpus is small and not representative of production traffic. The new operating point reduced observed false positives, but may change false-negative behavior; evaluate a larger labeled corpus before production use.
- Internal persistence endpoints are intentionally loopback-only. The current FastAPI-to-Node state bridge therefore assumes both services run on the same host. A multi-host deployment needs an authenticated private service-to-service channel.
- Cross-process serialization depends on MongoDB lease renewal. An extended MongoDB outage or lease loss can prevent reliable request coordination; monitor these failures and retry safely.
- Legacy blocked conversations require user-triggered rechecks. Existing block decisions are not automatically cleared.
- `start.ps1` is Windows-specific and assumes installed Node/Python dependencies and local model assets.

## Verdict

```text
environment:
  docker: UNAVAILABLE — Docker CLI is not installed; local MongoDB was reachable
  node: AVAILABLE — v24.20.0
  python: AVAILABLE — 3.14.7
  playwright: AVAILABLE — Chromium installed; browser E2E passed
  infra-tier: PRIMARY(local MongoDB) — Docker unavailable; existing local MongoDB used
  browser-tier: PRIMARY(Playwright) — 4 browser flows passed
startup: PASS — local health checks returned 200; Node started for validation
integration: PASS — exit_code: 0; passed: 10, failed: 0; Node + MongoDB + FastAPI flows
ai_engine: PASS — exit_code: 0; passed: 95, failed: 0
client: PASS — exit_code: 0; lint and production build succeeded
e2e: PASS — exit_code: 0; passed: 4, failed: 0; persistence, refusals, blocks, and legacy UI recovery
overall: PASS — all executed suites passed; detector corpus and deployment limitations remain
```
