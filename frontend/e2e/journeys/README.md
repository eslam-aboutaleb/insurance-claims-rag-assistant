# Tier C — End-to-end user journeys

The eight journeys from the Phase 0 stabilization plan (part A3),
run against the **live Docker stack with a real LLM**. They are
tagged `@journey` and excluded from the default `npm run e2e`
selection, so the fast unit-level e2e specs stay fast and the
journey run stays manual and explicit.

## Prerequisites

1. The full stack is up and healthy:

   ```bash
   docker compose up --build
   curl http://localhost:8000/api/v1/health   # {"status":"healthy"}
   curl http://localhost:3000                   # frontend serves
   ```

2. A real LLM provider key is configured in `.env`
   (`LITELLM_API_KEY` or whichever provider `LITELLM_MODEL` routes to).
   The journeys assert on real generated text, so a mock or stub
   LLM will not satisfy them.

3. Ports 3000 (frontend) and 8000 (backend) are free.

## Running

```bash
npm run e2e:journeys                 # from frontend/
# or
frontend/scripts/journeys.sh         # from anywhere
frontend/scripts/journeys.sh -g "Journey 1"   # filter, any playwright args pass through
```

The runner uses `playwright.journeys.config.ts`, which starts
`docker compose up --build frontend` (and its dependencies) if
nothing is listening on port 3000, and reuses a running stack
otherwise.

## The eight journeys

| #   | Journey                                  | Asserts                                                                                                                                                               |
| --- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Signup → chat → first message            | Empty state, streaming text renders, **sources badge visible without interaction** (project constraint), conversation appears in the sidebar                          |
| 2   | Policy coverage question                 | Grounded answer with citations; the sources list names the policy or its sections                                                                                     |
| 3   | New Chat (sidebar button and Ctrl/Cmd+N) | Messages cleared, session reset, URL back to `/chat`, both conversations persist in the sidebar                                                                       |
| 4   | Open a sidebar conversation              | Full history loads, read-only (input replaced by the archived-chat notice), URL updates, reload keeps the conversation                                                |
| 5   | Stop generation mid-stream               | "Stopped generating" toast, user message and partial assistant text retained                                                                                          |
| 6   | Retry after error                        | **Same idempotency key reused** (project constraint, verified from the intercepted `Idempotency-Key` headers), exactly one assistant message for the one user message |
| 7   | Cross-user negative                      | A second user guessing user A's conversation UUID gets the "Failed to load conversation" panel (backend 404)                                                          |
| 8   | Claim submission                         | Agent prepares the submission and asks the user to confirm; **records the baseline that no confirmation UI exists**                                                   |

## Artifacts

Each journey writes to `e2e/journeys/artifacts/<journey-slug>/`:

- `console.log` — browser console and page errors
- `network.har` — full network HAR
- `screenshot.png` — final page state
- `observations.json` — journey-specific recorded values (e.g. the
  idempotency keys observed in journey 6, the claim-UI count in
  journey 8)

The first successful run's artifacts are the Tier C record. Keep
them (or copy them out) before re-running; a re-run overwrites the
directory for that journey.

## Failure triage

- **Journey 1/2 sources assertions fail** — the answer had no
  citations. Check that policy ingestion ran on startup
  (`ingest_on_startup`) and that the embedding drain worker is
  running; a claim-less policy index still answers from
  `policy_chunks`.
- **Journey 5 stop fails** — the LLM answered faster than the
  stop button appeared. Re-run; the journey waits up to 60s for
  streaming to start.
- **Journey 6 retry fails** — check the intercepted
  `idempotency_keys` in `observations.json`; a mismatch means the
  frontend generated a new key on retry (a project-constraint
  violation).
- **Journey 8 fails** — the real LLM did not reach the
  `prepare_claim_submission` tool within 120s. Re-run; LLM
  behaviour varies. If it consistently fails, check the agent's
  tool dispatch in the backend logs.

## Journey 8 baseline (do not silently change)

Journey 8 records that **no claim-confirmation UI exists**: the
agent's `prepare_claim_submission` tool returns a
`confirmation_token` and tells the user to confirm in the UI, but
the frontend has no confirm screen. This is a known gap (see
`backend/README.md` § Known Gaps). If a confirmation UI is added
later, extend this journey to drive it; do not silently change the
baseline.
