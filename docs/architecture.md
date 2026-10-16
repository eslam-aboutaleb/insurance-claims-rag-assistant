# Architecture and Data Flow

## Architecture & Data Flow

### Complete Data Flow Diagram

```
+-----------------------------------------------------------------------------------------+
|                                    USER ENVIRONMENT                                     |
|                                                                                         |
|       +-------------------------------------------------------------------------+       |
|       |                           Web Browser (User)                            |       |
|       +------------------------------------+------------------------------------+       |
+--------------------------------------------|--------------------------------------------+
                                               |
                     HTTP / Chat Request       | (Port 3000 -> 8000 via CORS)
                                               v
+-----------------------------------------------------------------------------------------+
|                                  DOCKER COMPOSE MESH                                    |
|                                                                                         |
|  +-------------------------------------+     +---------------------------------------+  |
|  |           React Frontend          |     |            FastAPI Backend            |  |
|  |             (Port 3000)             |     |              (Port 8000)              |  |
|  |  * Auth (Signup/Signin)            |     |  * REST Endpoints                     |  |
|  |  * Modern Chat Interface            |     |  * JWT + Cookie Auth                   |  |
|  |  * Conversation History Sidebar     |     |  * Pydantic Validation                 |  |
|  |  * Source Citation Cards            |     |  * CORS Middleware                     |  |
|  |  * Tool Call Transparency Badges    |     |  * Rate Limiting (SlowAPI)             |  |
|  |  * Streaming SSE Responses          |     |  * Idempotent Retries                  |  |
|  |  * Mobile Responsive Design         |     |  * Alembic Migrations on Startup       |  |
|  +-------------------------------------+     +-------------------+-------------------+  |
|                                                                  |                      |
|                                                                  v                      |
|                                              +---------------------------------------+  |
|                                              |          ADK Agent Runtime            |  |
|                                              |  * Google ADK LlmAgent & Runner       |  |
|                                              |  * ADK session service per User       |  |
|                                              |  * memory or PostgreSQL-backed        |  |
|                                              |  * FIFO Session Eviction (max 500)    |  |
|                                              |  * Prompt Safety & Injection Defense  |  |
|                                              +-------------------+-------------------+  |
|                                                                  |                      |
|                                         +------------------------+-------------------+  |
|                                         | LiteLLM Provider Router                    |  |
|                                         | (openai/gpt-4o-mini, claude, gemini, etc.) |  |
|                                         +------------------------+-------------------+  |
|                                                                  |                      |
|                         +----------------------------------------+                      |
|                         | Tool Invocation & Dispatch Engine                             |
|                         +-------------------+--------------------+                      |
|                                             |                                           |
|                +----------------------------+----------------------------+              |
|                |                            |                            |              |
|                v                            v                            v              |
|  +---------------------------+  +------------------------+  +------------------------+  |
|  |     RAG Vector Search     |  |   Claim Status Lookup  |  |    Claim Submission    |  |
|  |     [query_policy]        |  |   [get_claim_status]   |  |  [prepare_claim +      |  |
|  |                           |  |                        |  |   confirm_claim]       |  |
|  |  Also used by:            |  |  Also used by:         |  |                        |  |
|  |  [search_claims]          |  |  [search_claims]       |  |  Two-step flow with    |  |
|  |  (claims hybrid search)   |  |  (claims hybrid search)|  |  confirmation_token    |  |
|  +-------------+-------------+  +-----------+------------+  +-----------+------------+  |
|                |                            |                           |               |
|                v                            v                           v               |
|  +---------------------------+  +------------------------+  +------------------------+  |
|  |    pgvector Vector Store  |  |    Postgres Database   |  |    Postgres Database   |  |
|  |  (policy_chunks table,    |  |  (users, conversations,|  |  (users, claims,       |  |
|  |   hybrid vector + PostgreSQL full-text search    |  |   claims, claim_       |  |   claim_submissions    |  |
|  |   RRF search)             |  |   submissions tables)  |  |   tables)              |  |
|  +---------------------------+  +------------------------+  +------------------------+  |
+-----------------------------------------------------------------------------------------+
```

### Core Architecture Flow

**Authentication & Session Flow:**

1. User navigates to the app and authenticates via `POST /api/v1/auth/signin` or `POST /api/v1/auth/signup`
2. Backend validates credentials (Argon2 password hashing), issues a JWT, and sets an HTTP-only session cookie
3. Subsequent requests include the JWT via cookie or `Authorization: Bearer` header
4. `get_current_user` dependency resolves the authenticated user and sets `current_user_id` ContextVar for downstream access

**Chat & Agent Flow:**

1. Authenticated user sends a message in the React chat UI
2. Frontend sends `POST /api/v1/chat` with `{message}`. `user_id` is resolved from the authenticated JWT.
3. Optional `Idempotency-Key` header enables safe retry without duplicate LLM calls (cached for 5 minutes)
4. FastAPI validates the request, checks idempotency cache, and delegates to `run_agent(user_id, message)`
5. The Agent (via LiteLLM -> OpenAI) decides which tool(s) to invoke
6. Tools execute: RAG query to pgvector, claim lookup in Postgres, or claim preparation/submission in Postgres
7. Agent produces final response with citations and tool results
8. Backend returns `{response, sources, tool_calls}` to the frontend
9. Conversation turn is persisted asynchronously to the database

**Streaming Flow (SSE):**

1. Frontend sends `POST /api/v1/chat/stream`
2. Backend returns a `StreamingResponse` with `text/event-stream` media type
3. ADK agent emits events as the response is generated
4. Backend transforms ADK events into stable frontend SSE contract with `text_delta` and `response_complete` events
5. Sources are accumulated during streaming and emitted in the final `response_complete` event
6. Conversation turn is persisted as a background task after stream completes

```
[User Browser] -> [React/Vite :3000] -> [FastAPI :8000] -> [ADK Agent + LiteLLM]
                                                          +-> [pgvector / Postgres] (Policy RAG)
                                                          +-> [Postgres] (claim lookup)
                                                          +-> [Postgres] (claim submit)
```

---


## How the Agent Works

The OmniCare assistant is powered by **Google ADK** (`LlmAgent`) and routed through **LiteLLM** to an OpenAI-compatible model. When a user sends a message, the backend does not hard-code tool selection. Instead, it performs one inference step: the LLM decides which tools, if any, are needed to answer the question.

**Flow:**

1. User authenticates via `POST /api/v1/auth/signin` and receives a JWT session cookie.
2. User sends a message to `POST /api/v1/chat` (or `/chat/stream` for SSE).
3. FastAPI validates the JWT, checks idempotency cache (if key provided), and delegates to `run_agent(user_id, message)`.
4. ADK starts an async event loop with `runner.run_async()`.
5. LiteLLM forwards the conversation to OpenAI (`gpt-4o-mini` by default).
6. The model can return a plain text answer, or emit `function_calls` for:
    - `query_policy` → Hybrid RAG search over pgvector policy chunks
    - `get_claim_status` → Owner-scoped Postgres claim lookup
    - `search_claims` → Natural language hybrid search over user's claims
    - `prepare_claim_submission` → Pydantic-validated claim preparation (returns confirmation_token)
    - `get_procedure` → Loads a domain procedure playbook (coverage-review, claim-triage) from the procedures index
7. The backend executes the requested tools, feeds results back to the model, and continues the loop until a final text response is produced.
8. The final response, together with `sources` and `tool_calls`, is returned to the frontend.
9. Conversation turn is persisted asynchronously to the database.

**Security notes:**

- Every tool uses `current_user_id` from the async ContextVar, never from user input, to prevent IDOR.
- The system prompt explicitly rejects prompt injection, persona hijacking, and out-of-scope requests.
- Claim submissions are validated with Pydantic before any database write.
- All database queries are scoped to the authenticated user to prevent horizontal privilege escalation.

---

