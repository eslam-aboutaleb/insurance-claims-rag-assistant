# OmniCare Financial - Customer Assistant Prototype

A full-stack AI-powered customer assistant for **OmniCare Financial**, an enterprise insurance company. Policyholders can authenticate, ask policy coverage questions (with citations), look up claim statuses, and submit new claims - all through a modern chat interface.

> **Note:** OmniCare Financial is a fictional company. This repository is an engineering showcase that ships with synthetic sample policy and claim data (`backend/app/data/`) generated for demonstration purposes; it is not affiliated with, endorsed by, or derived from any real insurance company or client engagement.

Powered by **Google Agent Development Kit (ADK)** and **LiteLLM**, the assistant autonomously resolves policy coverage inquiries using local **pgvector** vector retrieval (RAG), checks live claim status records, and facilitates new claim submissions with full schema validation and audit trails.

---

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

## Features

### Authentication & Security

- **JWT-based Authentication**: HTTP-only cookies with SameSite=Lax protection against CSRF
- **User Registration & Login**: Signup and signin endpoints with Argon2id password hashing
- **Timing-safe Password Comparison**: Prevents user enumeration via response timing attacks
- **Dual Token Support**: Accepts tokens from both Authorization header and HTTP-only cookie
- **Rate Limiting**: SlowAPI rate limiting (20 req/min for chat, 10 req/min for session reset)
- **Request Size Limits**: 5MB maximum request body size enforced via middleware
- **Standardized Error Envelope**: All errors return `{"error": {"code", "message", "details"}}`
- **CORS Protection**: Dynamic CORS configuration from validated settings, no wildcard origins with credentials

### AI Agent & Intelligence

- **Google ADK LlmAgent**: Autonomous agent with native tool orchestration and multi-turn session state
- **LiteLLM Routing**: Provider-agnostic LLM routing (OpenAI, Anthropic, Gemini, etc.)
- **Dynamic Model Selection**: Change models via single environment variable
- **Prompt Safety Guardrails**: System prompt rejects prompt injection, persona hijacking, and out-of-scope requests
- **Tool Selection by LLM**: The model autonomously decides which tools to invoke based on user intent

### RAG (Retrieval-Augmented Generation)

- **Hybrid Vector Search**: Combines pgvector L2 similarity with PostgreSQL full-text search using Reciprocal Rank Fusion (RRF)
- **Policy Document Ingestion**: Automatic chunking and embedding on startup (idempotent)
- **Claims History Search**: Natural language search over user's own claims history (owner-scoped)
- **Source Citations**: Every RAG response includes clickable source citations linking to policy sections
- **Configurable Thresholds**: Adjustable RAG distance threshold via settings

### Claims Management

- **Claim Status Lookup**: Retrieve real-time claim information by ID
- **Natural Language Claims Search**: "Have I filed any water damage claims?" - hybrid search over claims
- **Two-Step Claim Submission**:
  1. `POST /api/v1/claims/prepare` - Validates data and creates a pending submission with `confirmation_token`
  2. `POST /api/v1/claims/confirm` - Confirms and persists the claim after user approval
- **Pydantic Validation**: Strict schema validation (positive amounts, minimum description lengths, required fields)
- **Claim Citations**: Submitted claims include a citation reference for audit trails

### Conversation Management

- **Persistent Conversations**: All chat history stored in PostgreSQL with full message metadata
- **Conversation Sidebar**: Browse, select, and start new conversations
- **Message History**: Load full conversation history with sources and tool call details
- **Session Reset**: Clear agent context and start fresh conversations
- **Auto-generated Titles**: Conversation titles derived from the first user message
- **FIFO Session Eviction**: ADK sessions are bounded (max 500) with automatic eviction of oldest sessions

### Observability & Tracing

- **Structured Logs with Correlation Ids**: One JSON object per line, each keyed by the request's `X-Request-ID`, so a single user turn is followable across services
- **Per-Call Unit Economics**: Every model call records tokens, dollar cost, and latency; every tool invocation records its own latency and outcome
- **Operational Telemetry Endpoint**: `GET /api/v1/health/detailed` reports database reachability, model-call spend over the last hour, per-tool latency, embedding-outbox depth, and rate-limit rejections
- **LangSmith Tracing (opt-in)**: Set `LANGSMITH_TRACING_ENABLED=true` with a `LANGSMITH_API_KEY` and every agent turn becomes one trace at [smith.langchain.com](https://smith.langchain.com): the model calls it made, the tools those calls triggered, their arguments and results, and the tokens and cost of each model call, all nested under the turn. Traces are searchable by user, model, and request id. Tracing lives entirely in the observability layer -- the application imports `app.observability.tracing` and never the vendor behind it -- so the vendor can change without touching application code. See the environment variables table below

### Frontend Experience

- **ChatGPT-style Dark Theme**: Modern, clean dark mode interface
- **Markdown Rendering**: Rich text with syntax highlighting for code blocks
- **Source Citation Badges**: Expandable badges showing policy sections and claim references
- **Tool Call Transparency**: Collapsible tool details showing function name, arguments, and results
- **Real-time Streaming**: Server-Sent Events for live response generation
- **Copy Message**: One-click copy for assistant responses
- **Retry on Error**: Retry button for failed message sends
- **Responsive Mobile Design**: Touch swipe sidebar, mobile menu button, adaptive layouts
- **Loading States**: Spinner during auth hydration, skeleton states for history loading

---

## Quick Start (2 Minutes)

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- An [OpenAI API key](https://platform.openai.com/api-keys) (free tier is sufficient)

### Steps

1. **Clone the repository**

    ```bash
    git clone <repo-url>
    cd omnicare-financial
    ```

2. **Configure Environment Variables**

    ```bash
    cp .env.example .env
    ```

    Edit `.env` and add your OpenAI API key:

    ```ini
    OPENAI_API_KEY=sk-your-openai-api-key-here
    POSTGRES_PASSWORD=your-secure-password
    ```

3. **Build & Launch Containers**

    ```bash
    docker compose up --build
    ```

    Docker Compose builds and starts six services: the PostgreSQL 16 (pgvector) database (`db`), Redis (`redis`, shared idempotency and rate-limit state), the FastAPI backend, two drain workers — `embedding-drain` for the embedding-job outbox and `notification-drain` for the notification-job outbox — and the React/Vite frontend. The backend and both workers wait for the database health check; the frontend waits for the backend health check (`/api/v1/health`) to report `healthy` before accepting traffic. The database is published on `127.0.0.1:15432` and Redis on `127.0.0.1:16379` (both loopback only) to avoid colliding with a local PostgreSQL on 5432.

4. **Open the Application**
    Navigate to [http://localhost:3000](http://localhost:3000) in your browser. The backend will automatically:
    - Apply pending database migrations via Alembic
    - Ingest the policy document into pgvector on startup
    - Start accepting requests

---

## Framework Selection Rationale

| Technology                               | Selection Rationale                                                                                                                                           | Key Advantages                                                                                                                                                                                                                                                                                                                                                                                                           |
| :--------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Google ADK** _(Agent Development Kit)_ | **Enterprise Agent Framework** -- Standardized, resilient agent orchestration engine providing native session tracking, tool execution loops, and guardrails. | _ **Native Tool Orchestration**: Converts standard Python functions directly into model-consumable tool definitions.<br>_ **Multi-Turn Session State**: A swappable `BaseSessionService`, so in-process storage and the PostgreSQL-backed store share one contract and the runner is unchanged by the swap.<br>_ **Model Agnostic**: Seamlessly interfaces with third-party providers via LiteLLM.<br>_ **Clean Pattern**: Separates system instructions, tool definitions, and runtime execution. |
| **LiteLLM**                              | **Universal LLM Proxy & Router** -- Decouples the core agent code from vendor-specific LLM APIs.                                                              | _ **100+ Provider Support**: Switch effortlessly between OpenAI, Anthropic, Google Gemini, Azure, and open-source models.<br>_ **Zero Code Changes**: Change the model with a single environment variable (`LLM_MODEL`).<br>\* **Standardized Input/Output**: Normalizes API schemas, cost tracking, and error handling across providers.                                                                                |
| **pgvector**                          | **PostgreSQL Vector Extension** -- Native pgvector extension inside PostgreSQL for hybrid vector + PostgreSQL full-text search with Reciprocal Rank Fusion. | _ **Unified Storage**: Embeddings and keyword search live in the same Postgres instance as claims and conversations.<br>_ **Hybrid Search**: Combines vector similarity (HNSW) with PostgreSQL full-text search (tsvector/tsquery) using RRF for robust retrieval.<br>_ **No External Service**: Eliminates the need for a separate vector database process or Docker volume.                                                                                   |
| **ragit** _(GitHub)_           | **Domain-Agnostic RAG Toolkit** -- Published package (pinned git dependency, `ragit-toolkit[pgvector] @ v0.3.1`) carrying every generic RAG primitive: chunking, embeddings, vector stores, ingestion, evaluation, and the job outbox. | _ **Clean Separation**: OmniCare-specific bindings live in `backend/app/domain/`; generic machinery lives in `ragit`.<br>_ **Library Isolation**: ragit never imports `app.*`.<br>_ **Reusable**: Any FastAPI/SQLAlchemy service can adopt it; see [github.com/eslam-aboutaleb/ragit](https://github.com/eslam-aboutaleb/ragit). |

### The ragit Library

The RAG subsystem is extracted into an installable, domain-agnostic library so the retrieval machinery can be reused (and tested) independently of OmniCare's schema. `ragit` is published from [github.com/eslam-aboutaleb/ragit](https://github.com/eslam-aboutaleb/ragit). The backend pins it as a git dependency with the `pgvector` extra in `backend/pyproject.toml`:

```
"ragit-toolkit[pgvector] @ git+https://github.com/eslam-aboutaleb/ragit.git@v0.3.1",
```

| ragit module           | Responsibility                                                                                              |
| :----------------------- | :---------------------------------------------------------------------------------------------------------- |
| `ragit.chunking`        | `MarkdownSectionChunker`, `SlidingWindowChunker`, and the chunker registry                                  |
| `ragit.embeddings`      | `EmbeddingFunction` protocol, LiteLLM implementation, dimension validation, provider registry               |
| `ragit.stores`          | Vector stores: `PgVectorStore` (hybrid vector + PostgreSQL full-text search with RRF), `QdrantStore`, `ChromaStore`, `FaissStore`, and `InMemoryVectorStore` |
| `ragit.ingestion`       | `IngestionPipeline` (hash/compare/retire/chunk/embed/upsert), document sources, snapshot versioning, locking |
| `ragit.jobs`            | Outbox `EmbeddingJobStore` protocol, `process_pending_jobs`, `reclaim_stale_jobs`, drainer CLI              |
| `ragit.evaluation`      | RAG evaluation harness: retrieval metrics (precision/recall@k, MRR) and LLM-judged answer metrics           |
| `ragit.validation`      | SQL identifier allowlist and embedding dimension checks                                                     |

**Domain adapters** (`backend/app/domain/`) bind ragit to OmniCare's tables: `domain/policies/` owns policy versioning and ingestion, `domain/claims/` owns claim ingestion, the embedding-job outbox, and owner-scoped claim retrieval, `domain/procedures/` owns the second index of domain procedure playbooks, and `domain/embeddings.py` wires the embedding function and vector store. The claims adapter always passes `owner_id` as a retrieval filter, so cross-user data leakage is structurally impossible.

**Embedding drain worker** (`embedding-drain` service): claim embeddings never block claim submission. `POST /api/v1/claims/confirm` writes the claim and its `embedding_jobs` row in the same transaction (outbox pattern); the drainer worker claims pending jobs with `FOR UPDATE SKIP LOCKED`, generates the embedding, and upserts it into pgvector. Multiple drainer replicas scale without coordination, and jobs stuck in `processing` past `JOB_STALE_AFTER_SECONDS` are reclaimed automatically.

### The Procedures Index

`domain/procedures/` is a **second document index next to the policy one**. It holds domain *procedure playbooks* — currently `coverage-review.md` (the coverage determination checklist) and `claim-triage.md` (intake triage) in `backend/app/data/procedures/` — and the agent loads one on demand through the `get_procedure` tool.

"Skills" here means playbooks retrieved as documents, for two concrete reasons: ADK 1.3 has no loader for Claude-style on-disk skill bundles, and the app drives models through `LiteLlm` on ADK's chat-completions path rather than the Responses API, so the OpenAI Skills API's `skill_reference` is not consumable. The payoff is token cost — the system prompt stays as it is, and a compliance update is an edit to a markdown file instead of a prompt edit plus a deploy.

Each playbook carries an explicit **When to invoke** and **Do NOT invoke for** section. That is what makes retrieval-driven loading behave like a skill rather than a text dump: the model gets the boundary along with the procedure.

Running it:

```bash
# Re-index the procedures corpus (idempotent: an unchanged playbook is
# detected by snapshot comparison and is not re-embedded)
docker-compose exec backend python -m app.domain.procedures.ingestion

# Ingest automatically on startup (default: enabled)
OMNICARE_INGEST_PROCEDURES=true docker-compose up -d backend

# Skip it when booting against a database that is already indexed
OMNICARE_INGEST_PROCEDURES=false docker-compose up -d backend

# Tests (against a dedicated test database)
cd backend && python -m pytest tests/test_procedures_retrieval.py -q -p no:randomly
```

### Notifications

Claim transitions have to tell the policyholder something happened. `domain/notifications/` is a **second outbox next to the embedding one**, and it obeys the same rule: the API writes the job row inside the claim's own transaction, and the `notification-drain` worker is the only thing that materialises the message in the recipient's feed. A provider that is slow or down therefore becomes a delayed notification instead of a failed claim transition, and the drainer claims rows with `FOR UPDATE SKIP LOCKED` so extra replicas scale without coordination.

The invariant that follows is sharper than referential integrity: a notification job must commit **in the same transaction** as the claim change it describes. A job row that survives a rolled-back status change tells a policyholder about a transition that never happened, and there is no way to take that back once it has been read. The claim change is the fact; the notification is a promise about it, so the two land together or neither lands. `enqueue_notification_job(session=...)` flushes into the caller's session for exactly this reason, and calling it without one is only correct where the claim change is already durable.

The title and body are rendered from a per-job-type template table (`render_notification`), not by a model call. Three reasons, all of them cost-shaped: a notification is a fixed string about a stored fact, so a model invocation per delivery buys nothing; the worker must not spend money to say "your claim moved"; and two drainers running two prompt versions would otherwise show the same claimant two different phrasings of the same event.

### Admin & Audit

`app/api/v1/admin.py` is the operator's read surface over the data no policyholder may see: a cross-owner claims queue, embedding-outbox depth and dead letters, the paged audit trail, and the feed of answers users marked down. Every route sits behind `require_role("admin")`, and there is deliberately no admin UI and no write surface — this is the read API a console consumes, not something a browser can drive.

The response envelopes (`AdminClaimPage`, `EmbeddingJobPage`, `AuditEventPage`, `DownRatedAnswerPage`) live in that module rather than in `app/schemas/models.py` because they describe rows owned by other modules. The shared schema module is the frozen contract surface of the public API; an admin page shape has no business appearing in it, and putting one there would freeze an internal shape by accident.

`app/audit.py` records the trail itself from two directions. `record_audit` is called by a route inside the transaction that performs the audited action, so the entry commits with it — authentication events and feedback submissions go this way. The claim lifecycle (submission, status transitions, withdrawal) is recorded from a `ClaimEvent` flush listener instead, because that half is owned by code the audit module must not reach into, and a listener also catches a transition written by any future caller. Both paths share one trade: an audit write that fails is logged and swallowed, because an outage of the trail must not take the business action down with it. That means an entry can be missing during an incident, which is the same trade every append-only log makes and the reason this table is not a foreign key to success.

---

## API Reference

### Authentication Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/auth/signup` | Register a new user account (Argon2 password hashing) |
| `POST` | `/api/v1/auth/signin` | Authenticate and receive JWT session cookie |
| `POST` | `/api/v1/auth/logout` | Clear session cookie |
| `GET` | `/api/v1/auth/me` | Get current authenticated user info |

### Chat Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/chat` | Process chat message through AI agent (with idempotency support) |
| `POST` | `/api/v1/chat/stream` | Stream agent response as Server-Sent Events (SSE) |
| `POST` | `/api/v1/chat/reset` | Reset conversation session for new context |

### Conversation Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/chat/conversations` | List authenticated user's conversations |
| `GET` | `/api/v1/chat/conversations/{conversation_id}` | Get full message history for a conversation |

### Claims Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/claims/prepare` | Validate and create a pending claim submission (returns `confirmation_token`) |
| `POST` | `/api/v1/claims/confirm` | Confirm and submit a pending claim |
| `POST` | `/api/v1/claims/cancel` | Cancel a pending claim submission before it is confirmed |
| `GET` | `/api/v1/claims` | List the authenticated user's claims |
| `GET` | `/api/v1/claims/{claim_id}` | Read one claim, owner-scoped |
| `POST` | `/api/v1/claims/{claim_id}/status` | Move a claim to a new status (owner-scoped; only withdrawal is a policyholder's own transition) |
| `POST` | `/api/v1/claims/{claim_id}/withdraw` | Withdraw a claim |

### RAG Evaluation Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/rag/evaluate` | Score retrieval and answer quality for the policy corpus |
| `GET` | `/api/v1/rag/dataset` | Read the evaluation dataset |

### Feedback Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/feedback` | Record a thumbs-up or thumbs-down rating on the caller's own answer |
| `DELETE` | `/api/v1/feedback/{message_id}` | Remove the caller's rating on one message |

### Notification Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/notifications` | Read the authenticated user's in-app feed |
| `POST` | `/api/v1/notifications/{notification_id}/read` | Mark one notification as read |

### Admin Endpoints

Every route below requires the `admin` role; the response envelopes are defined in `app/api/v1/admin.py`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/admin/claims` | The cross-owner claims queue, paged |
| `GET` | `/api/v1/admin/embedding-jobs` | Embedding-outbox depth and dead-lettered jobs |
| `GET` | `/api/v1/admin/audit` | The paged audit trail |
| `GET` | `/api/v1/admin/feedback` | The feed of answers users marked down |

### Health & Docs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Service health check |
| `GET` | `/api/v1/health/detailed` | Database probe plus model-call spend, tool latency, outbox depth, rate-limit rejections, and the tracing configuration. Registered separately from `/health` so that pinned contract and its response shape stay untouched; a degraded sink is reported in the body, not as a failure. |
| `GET` | `/docs` | Swagger UI interactive API documentation |
| `GET` | `/redoc` | ReDoc API documentation |

---

## Sample cURL Requests

### 1. Health Check

```bash
curl http://localhost:8000/api/v1/health
# -> {"status": "healthy"}
```

### 2. User Signup

```bash
curl -X POST http://localhost:8000/api/v1/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john@example.com",
    "password": "securepassword123"
  }'
```

### 3. User Signin

```bash
curl -X POST http://localhost:8000/api/v1/auth/signin \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john@example.com",
    "password": "securepassword123"
  }'
# -> {"access_token": "...", "token_type": "bearer", "user_id": "..."}
```

### 4. Policy Coverage Question (RAG)

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -d '{
    "message": "What is covered under water damage?"
  }'
```

### 5. Claim Status Lookup

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -d '{
    "message": "What is the status of claim CLM-8821?"
  }'
```

### 6. Submit a New Claim (Two-Step)

```bash
# Step 1: Prepare claim submission
curl -X POST http://localhost:8000/api/v1/claims/prepare \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -d '{
    "policy_number": "POL-1092",
    "claim_type": "Water Damage",
    "amount": 5000.00,
    "description": "A pipe burst in my kitchen yesterday causing flooding and water damage to the floor and cabinets."
  }'
# -> {"confirmation_token": "...", "status": "pending", "expires_at": "..."}

# Step 2: Confirm claim submission
curl -X POST http://localhost:8000/api/v1/claims/confirm \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -d '{
    "confirmation_token": "<token-from-step-1>"
  }'
```

### 7. Idempotent Request

```bash
# First request
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -H "Idempotency-Key: my-request-id-123" \
  -d '{"message": "What is my deductible?"}'

# Retry with same Idempotency-Key returns cached response
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -H "Idempotency-Key: my-request-id-123" \
  -d '{"message": "What is my deductible?"}'
# Response includes header: X-Idempotent-Replayed: true
```

---

## Environment Variables

Configure these settings in `.env` (or pass via container environment):

| Variable                 | Default Value                 | Required | Purpose & Description                                                                               |
| :----------------------- | :---------------------------- | :------: | :-------------------------------------------------------------------------------------------------- |
| `OPENAI_API_KEY`         | _(None)_                      | **Yes**  | OpenAI API secret key required when using OpenAI models.                                            |
| `OPENAI_API_BASE`        | `""`                          |    No    | Optional OpenAI API base URL override for LiteLLM routing.                                          |
| `LLM_MODEL`              | `openai/gpt-4o-mini`          |    No    | Model descriptor for LiteLLM router (e.g. `openai/gpt-4o`, `anthropic/claude-3-5-sonnet-20241022`). |
| `EMBEDDING_MODEL`        | `text-embedding-3-small`      |    No    | Embedding model name used by LiteLLM for pgvector embeddings.                                                       |
| `DATABASE_URL`           | _(None)_                      | **Yes**  | SQLAlchemy async PostgreSQL connection string.                                                       |
| `POSTGRES_USER`          | `omnicare`                    |    No    | PostgreSQL username for the database service.                                                       |
| `POSTGRES_PASSWORD`      | _(None)_                      | **Yes**  | PostgreSQL password for the database service. Must be set for the container to start.               |
| `POSTGRES_DB`            | `omnicare`                    |    No    | PostgreSQL database name.                                                                           |
| `POLICY_FILE_PATH`       | `./app/data/sample_policy.md` |    No    | Path to the markdown insurance policy document for RAG ingestion.                                   |
| `HOST`                   | `0.0.0.0`                     |    No    | IP address interface binding for FastAPI Uvicorn server.                                            |
| `PORT`                   | `8000`                        |    No    | HTTP port for the FastAPI backend server.                                                           |
| `VITE_API_URL`           | `http://localhost:8000`       |    No    | Backend base URL accessed by the React client browser interface.                                    |
| `JWT_SECRET_KEY`          | _(auto-generated)_            |    No    | Secret key for signing JWT tokens. Auto-generated in development; must be set in production.        |
| `LOG_LEVEL`              | `INFO`                        |    No    | Logging verbosity level (DEBUG, INFO, WARNING, ERROR, CRITICAL).                                    |
| `RAG_DISTANCE_THRESHOLD` | `1.3`                         |    No    | Maximum L2 distance for RAG vector search (lower = stricter matching).                              |
| `OMNICARE_INGEST_PROCEDURES` | `true`                    |    No    | Whether to index the procedure playbooks during startup. Ingestion is idempotent, so the cost after the first run is a file read and a snapshot comparison. |

Compose-level variables (read by `docker-compose.yml`, not by `Settings`):

| Variable | Default Value | Required | Purpose & Description |
| :------- | :------------ | :------: | :-------------------- |
| `POSTGRES_HOST_PORT` | `15432` | No | Host port the `db` container publishes on, bound to loopback only. Keep in sync with `DATABASE_URL`; it is not 5432 so it does not collide with a locally installed PostgreSQL. |
| `REDIS_HOST_PORT` | `16379` | No | Host port the `redis` container publishes on, loopback only. |

Observability:

| Variable | Default Value | Required | Purpose & Description |
| :------- | :------------ | :------: | :-------------------- |
| `OBSERVABILITY_ENABLED` | `true` | No | Master switch for the request-id correlation middleware, structured log records, per-tool latency instrumentation, and `GET /api/v1/health/detailed`. `false` removes the hooks themselves rather than only silencing their output. |
| `LLM_COST_TRACKING_ENABLED` | `true` | No | Whether the LiteLLM callback that records tokens, dollar cost, and latency per model call is registered. Disable only when an external tracer already collects the same data. |
| `LOG_FORMAT` | `json` | No | Log record encoding. `json` writes one JSON object per line with the correlation id as a field, which is what a log aggregator expects; `text` keeps the single-line layout for reading logs by eye. |

LangSmith tracing (opt-in):

| Variable | Default Value | Required | Purpose & Description |
| :------- | :------------ | :------: | :-------------------- |
| `LANGSMITH_TRACING_ENABLED` | `false` | No | Whether every agent turn is traced to LangSmith: one trace per turn, with the model calls and tool calls it made nested under it. Off by default because prompts and responses are sent to a third-party service. Gated by `OBSERVABILITY_ENABLED` like every other hook. |
| `LANGSMITH_API_KEY` | `""` | No | LangSmith API key (starts with `lsv2_`), created under Settings -> API Keys. Blank leaves tracing off even when enabled, and the startup logs say so. |
| `LANGSMITH_PROJECT` | `omnicare-financial` | No | LangSmith project the traces are reported to; created on demand on the first traced run. |
| `LANGSMITH_ENDPOINT` | `""` | No | LangSmith API base URL. Blank uses the LangSmith cloud (`https://api.smith.langchain.com`); set it for a self-hosted or region-specific deployment. |
| `LANGSMITH_WORKSPACE_ID` | `""` | No | Workspace (organization) id, only needed for keys scoped to one workspace of a multi-workspace organization. |

Redis and shared stores:

| Variable | Default Value | Required | Purpose & Description |
| :------- | :------------ | :------: | :-------------------- |
| `REDIS_URL` | `""` | No | Redis connection URI (e.g. `redis://redis:6379/0`). Empty means no Redis is configured and every store below keeps its in-process behaviour. Redis carries idempotency and rate-limit state only, never sessions. |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | `1` | No | Seconds to wait for a Redis TCP connection before the store gives up and falls back to in-process state. Kept short so an unresponsive Redis never stalls a request. |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `2` | No | Seconds to wait for a Redis reply before the command is abandoned and the store falls back to in-process state. |
| `IDEMPOTENCY_STORAGE` | `memory` | No | Where successful chat responses are cached for replay: `memory` (per process) or `redis` (one cache across every replica). A Redis outage degrades to `memory` rather than failing the request. |
| `RATE_LIMIT_STORAGE` | `memory` | No | Where rate-limit counters live: `memory` counts per process (so N replicas allow N x the quota), `redis` makes the per-minute limit global. The limiter fails open on a Redis outage. |

Session storage (PostgreSQL, not Redis):

| Variable | Default Value | Required | Purpose & Description |
| :------- | :------------ | :------: | :-------------------- |
| `SESSION_STORAGE` | `memory` | No | Where ADK conversation sessions live: `memory` (in-process, lost on restart) or `database` (the `agent_sessions` and `agent_session_events` tables), which lets any replica continue any conversation. Switching a running deployment abandons the sessions that process held. |
| `SESSION_HISTORY_TURNS` | `20` | No | Most-recent persisted turns replayed into a session that has none of its own — a fresh session, a new replica, or the first turn after the storage swap. |
| `SESSION_HISTORY_COMPACTION_TURNS` | `60` | No | Older turns folded into the single compaction summary that precedes the replayed history, so a long conversation stays inside the model's context window. |

Background workers:

| Variable | Default Value | Required | Purpose & Description |
| :------- | :------------ | :------: | :-------------------- |
| `INGEST_ON_STARTUP` | `true` | No | Whether to index the policy document during API startup. Set `false` to boot against an already-indexed database and skip the cost on every boot. |
| `EMBEDDING_DRAIN_INTERVAL_SECONDS` | `5` | No | Idle sleep of the embedding drainer between empty passes. |
| `EMBEDDING_DRAIN_BATCH_SIZE` | `10` | No | Embedding jobs the drainer claims per pass. |
| `NOTIFICATION_DRAIN_INTERVAL_SECONDS` | `5` | No | Idle sleep of the notification drainer between empty passes. These only affect how quickly a claimant is told their claim moved; delivery never runs on the request path. |
| `NOTIFICATION_DRAIN_BATCH_SIZE` | `10` | No | Notification jobs the drainer delivers per pass. |
| `JOB_STALE_AFTER_SECONDS` | `300` | No | Seconds after which a job stuck in `processing` is treated as abandoned by a dead worker and reclaimed to `pending`. Guards both outbox drainers. |
| `WORKER_ID` | _(host-pid)_ | No | Identity stamped onto jobs a drainer claims, so concurrent drainers are distinguishable in `locked_by`. |

Testing:

| Variable | Default Value | Required | Purpose & Description |
| :------- | :------------ | :------: | :-------------------- |
| `OMNICARE_TEST_DATABASE_URL` | _(None)_ | No | Database the suite runs against. It must contain `test` in its name, or the run aborts: the per-test fixture deletes every row in every application table, so pointing it at the stack's own database would destroy live development data. |

---

## Running Tests

OmniCare Financial includes automated tests with Pytest covering authentication, RAG ingestion, vector retrieval, agent tool execution, idempotency, and API endpoints. The backend suite runs against a dedicated test database (`OMNICARE_TEST_DATABASE_URL`) and exercises core paths and corner cases (empty inputs, invalid parameters, locking, stale-job reclaim, versioning, and cross-user isolation). It is organised in three layers: a tier of API-level suites under `tests/`, domain-level tests under `tests/domain/`, database-backed integration tests under `tests/integration/`, and a frozen HTTP contract suite under `tests/contract/`. The generic RAG machinery is covered by the ragit package's own test suite, published from [github.com/eslam-aboutaleb/ragit](https://github.com/eslam-aboutaleb/ragit).

### Run Tests Locally

```bash
# Backend tests (from the repository root; the backend
# resolves ragit from the pinned git dependency)
cd backend
uv pip install -e .
python -m pytest tests/ -v
```

### Run Tests with Coverage Report

```bash
cd backend
python -m pytest tests/ --cov=app --cov-report=term-missing
```

Line coverage of `backend/app` is measured with `pytest --cov=app` and the suite is held to a >90% target. Run it before opening a pull request rather than trusting a number quoted here: coverage moves with every change, so a figure committed to this file is stale the moment it is written.

### Run Tests Inside Docker Container

```bash
docker-compose exec backend pytest tests/ -v
```

### Key Test Files

| Test File | Coverage |
|-----------|----------|
| `tests/test_auth_security.py` | Authentication flows, JWT validation, password hashing |
| `tests/test_chat.py` | Non-streaming chat endpoint, idempotency, error handling |
| `tests/test_chat_stream.py` | Streaming SSE endpoint, event transformation |
| `tests/test_idempotency.py` | Idempotency key caching, body hash verification |
| `tests/test_rag.py` | Policy retrieval, hybrid search, RRF scoring, chunking |
| `tests/test_claim_status.py` | Claim status lookup, owner scoping |
| `tests/test_submit_claim.py` | Claim preparation and confirmation flow |
| `tests/domain/test_search_claims.py` | Natural language claims search, owner-scoped |
| `tests/test_procedures_retrieval.py` | Procedure playbook ingestion, idempotency, and `get_procedure` scoping |
| `tests/test_conversation_store.py` | Conversation persistence, message metadata |
| `tests/test_security_isolation.py` | Cross-user data isolation, horizontal privilege escalation |
| `tests/test_llm_e2e.py` | End-to-end LLM integration tests |

---

## Project Structure

```
omnicare-financial/
+-- .env.example                    # Template for environment configuration
+-- .gitignore                      # Git exclusion rules (Python, Node, Docker)
+-- .dockerignore                   # Root build context filter (backend images)
+-- .pre-commit-config.yaml         # Pre-commit hooks (ruff, prettier)
+-- docker-compose.yml              # Multi-container orchestration & networking
+-- docker-compose.dev.yml          # Development overrides
+-- README.md                       # Comprehensive project documentation
+-- sonar-project.properties        # SonarQube configuration
+-- Makefile                        # Baseline harness targets (Tier A/B/C)
|
+-- backend/                        # FastAPI + Google ADK Agent Service
|   +-- Dockerfile                  # Python 3.11-slim production image (uv-only)
|   +-- Dockerfile.dev              # Development image with hot reload
|   +-- .dockerignore               # Backend build context filter
|   +-- .env.example                # Backend-side environment template
|   +-- pyproject.toml              # Build tool specifications (incl. the ragit git pin)
|   +-- README.md                   # Backend engineering guide
|   +-- alembic.ini                 # Alembic CLI configuration
|   +-- alembic/                    # Database migration version control
|   |   +-- env.py                  # Alembic environment configuration
|   |   +-- script.py.mako          # Migration template
|   |   +-- versions/               # Migration scripts (19 migrations)
|   |       +-- 6f253a5df2e9_initial_schema.py
|   |       +-- a1b2c3d4e5f6_add_pgvector_embeddings.py
|   |       +-- 822df1979a12_add_conversation_messages_claim_.py
|   |       +-- b6f2c8a4d1e7_create_notification_jobs_and_feed_tables.py
|   |       +-- b8c3d9e4f1a6_add_audit_events_and_user_role.py
|   |       +-- c9e1a4b6d8f0_create_answer_feedback_table.py
|   |       +-- e3b9c4d7a1f2_create_procedure_index_tables.py
|   |       +-- f1a4b7c2d9e3_create_eval_runs_table.py
|   |       +-- f3c8a1d7b2e9_create_claim_events_table.py
|   |       +-- fc60119aea11_merge_policy_and_claim_heads.py
|   |       +-- ...                 # the remaining migration scripts
|   +-- app/
|   |   +-- __init__.py             # Package init
|   |   +-- config.py               # Pydantic Settings configuration loader
|   |   +-- main.py                 # FastAPI application, CORS, lifespan hooks
|   |   +-- database.py             # SQLAlchemy async engine & session factory
|   |   +-- auth.py                 # JWT auth, Argon2 hashing, cookie management
|   |   +-- audit.py                # Append-only audit trail + ClaimEvent flush listener
|   |   +-- middleware.py           # Request size limit middleware
|   |   +-- rate_limiter.py         # SlowAPI rate limiting configuration
|   |   +-- idempotency.py          # Request idempotency cache with TTL
|   |   +-- agent/                  # Google ADK Agent definition
|   |   |   +-- __init__.py         # Agent package init
|   |   |   +-- agent.py            # LlmAgent initialization, session runner, event loop
|   |   |   +-- registry.py         # Tool registry (ToolSpec/build_default_registry)
|   |   |   +-- context.py          # Async context variables (current_user_id)
|   |   |   +-- prompts.py          # System instructions & security injection defenses
|   |   |   +-- conversation_store.py # Conversation persistence helpers
|   |   |   +-- tools/              # Agent tools
|   |   |       +-- __init__.py     # Tools package init
|   |   |       +-- policy_rag.py   # query_policy tool (RAG search)
|   |   |       +-- claim_status.py # get_claim_status tool (owner-scoped lookup)
|   |   |       +-- search_claims.py # search_claims tool (natural language claims search)
|   |   |       +-- submit_claim.py # prepare_claim_submission tool + submit_claim_internal
|   |   +-- api/                    # REST API endpoints
|   |   |   +-- __init__.py         # API package init
|   |   |   +-- v1/
|   |   |       +-- __init__.py     # API v1 package init
|   |   |       +-- router.py       # API v1 route aggregator
|   |   |       +-- chat.py         # POST /api/v1/chat + /chat/reset endpoints
|   |   |       +-- chat_stream.py  # POST /api/v1/chat/stream (SSE)
|   |   |       +-- health.py       # GET /api/v1/health check endpoint
|   |   |       +-- auth.py         # POST /signup, /signin, /logout, /me
|   |   |       +-- conversations.py # GET /chat/conversations, /chat/conversations/{id}
|   |   |       +-- claims.py       # Claim lifecycle: prepare, confirm, cancel, list, get, status, withdraw
|   |   |       +-- feedback.py     # POST /feedback, DELETE /feedback/{message_id}
|   |   |       +-- notifications.py # GET /notifications, POST /notifications/{id}/read
|   |   |       +-- admin.py        # GET /admin/* (role-gated queue, jobs, audit, feedback)
|   |   |       +-- rag_eval.py     # POST /rag/evaluate, GET /rag/dataset
|   |   +-- domain/                 # Domain adapters over ragit
|   |   |   +-- __init__.py         # Domain package init
|   |   |   +-- embeddings.py       # EmbeddingFactory + vector store wiring
|   |   |   +-- evaluation.py       # RAG evaluation CLI runner
|   |   |   +-- evaluation_cli.py   # Command-line entry point for the evaluation run
|   |   |   +-- evaluation_runs.py  # EvalRun persistence
|   |   |   +-- claims/             # Claims domain bindings
|   |   |   |   +-- __init__.py     # Claims package init
|   |   |   |   +-- ingest.py       # Claim ingestion into the claims vector store
|   |   |   |   +-- outbox.py       # Embedding-job outbox (SQLAlchemy store + processor)
|   |   |   |   +-- retriever.py    # Claims hybrid retrieval (owner-scoped)
|   |   |   |   +-- tools.py        # search_claims tool binding
|   |   |   +-- policies/           # Policies domain bindings
|   |   |   |   +-- __init__.py     # Policies package init
|   |   |   |   +-- ingestion.py    # Policy versioning + ingestion pipeline
|   |   |   |   +-- retriever.py    # Policy hybrid retrieval
|   |   |   |   +-- tools.py        # query_policy tool binding
|   |   |   +-- procedures/         # Procedure playbook bindings (second index)
|   |   |   |   +-- __init__.py     # Procedures package init
|   |   |   |   +-- ingestion.py    # Procedure corpus ingestion + startup gate
|   |   |   |   +-- store.py        # ProcedureVersionStore (ragit VersionStore)
|   |   |   |   +-- retrieval.py    # Procedure hybrid retrieval
|   |   |   |   +-- tools.py        # get_procedure tool binding
|   |   |   +-- notifications/      # Notification outbox bindings
|   |   |       +-- __init__.py     # Notifications package init
|   |   |       +-- outbox.py       # Notification-job store, templates, and drain wiring
|   |   +-- data/                   # Policy documents and mock claims
|   |   |   +-- sample_policy.md    # Source policy document for RAG
|   |   |   +-- mock_claims.json    # Baseline claims for local development and tests
|   |   |   +-- procedures/         # Domain procedure playbooks
|   |   |       +-- coverage-review.md # Coverage determination procedure
|   |   |       +-- claim-triage.md    # Intake triage procedure
|   |   +-- models/                 # SQLAlchemy ORM models
|   |   |   +-- __init__.py         # Models package init
|   |   |   +-- base.py             # Declarative base with common columns
|   |   |   +-- user.py             # User model (Argon2 password hashes)
|   |   |   +-- claim.py            # Claim model
|   |   |   +-- claim_submission.py # ClaimSubmission model (pending claims)
|   |   |   +-- claim_event.py      # ClaimEvent model (claim lifecycle history)
|   |   |   +-- conversation.py     # Conversation model (JSONB messages)
|   |   |   +-- conversation_message.py # ConversationMessage model (normalized)
|   |   |   +-- answer_feedback.py  # AnswerFeedback model (per-user answer ratings)
|   |   |   +-- audit_event.py      # AuditEvent model (append-only audit trail)
|   |   |   +-- notification_job.py # NotificationJob + Notification models (feed)
|   |   |   +-- policy.py           # Policy model
|   |   |   +-- policy_chunk.py     # PolicyChunk model
|   |   |   +-- policy_version.py   # PolicyVersion model (ingestion snapshots)
|   |   |   +-- procedure_version.py # ProcedureVersion model (playbook snapshots)
|   |   |   +-- policy_ingestion_meta.py # Per-source ingestion metadata
|   |   |   +-- embedding_job.py    # EmbeddingJob model
|   |   +-- observability/          # Request-id, structured logs, metrics, telemetry, tracing
|   |   |   +-- __init__.py         # Observability package init
|   |   |   +-- request_context.py  # Correlation-id ContextVar
|   |   |   +-- logging_config.py   # Structured record formatter
|   |   |   +-- metrics.py          # Bounded in-process metric store
|   |   |   +-- middleware.py       # RequestIdMiddleware
|   |   |   +-- llm_callback.py     # LiteLLM success/failure cost + latency hooks
|   |   |   +-- tracing.py          # Tracing API: the only module app code imports for tracing
|   |   |   +-- langsmith.py        # LangSmith implementation behind the tracing API
|   |   |   +-- tool_instrumentation.py # Per-tool call and latency instrumentation
|   |   |   +-- routes.py           # GET /api/v1/health/detailed
|   |   +-- schemas/                # Pydantic data schemas
|   |   |   +-- __init__.py         # Schemas package init
|   |   |   +-- models.py           # Request, response, and claim validation models
|   |   +-- workers/                # Background workers
|   |       +-- __init__.py         # Workers package init
|   |       +-- embedding_drain.py  # Outbox drainer (FOR UPDATE SKIP LOCKED)
|   |       +-- notification_drain.py  # Notification outbox drainer
|   +-- tests/
|   |   +-- conftest.py             # Shared fixtures (both-sided per-test database reset)
|   |   +-- baselines/              # RAG baseline snapshots
|   |   |   +-- rag_baseline.json
|   |   +-- test_health.py          # Health endpoint tests
|   |   +-- test_auth_security.py   # Authentication & security tests
|   |   +-- test_chat.py            # Chat endpoint tests
|   |   +-- test_chat_stream.py     # Streaming chat endpoint tests
|   |   +-- test_idempotency.py     # Idempotency tests
|   |   +-- test_rag.py             # RAG retrieval tests
|   |   +-- test_claim_status.py    # Claim lookup tests
|   |   +-- test_submit_claim.py    # Claim submission tests
|   |   +-- test_conversation_store.py # Conversation persistence tests
|   |   +-- test_security_isolation.py # Cross-user isolation tests
|   |   +-- test_llm_e2e.py         # End-to-end LLM integration tests
|   |   +-- test_procedures_retrieval.py # Procedure playbook ingestion & get_procedure scoping
|   |   +-- test_agent.py           # Agent registry, tools, and prompt contract surface
|   |   +-- test_prompt_contract.py # System prompt contract (guardrails and boundaries)
|   |   +-- test_config.py          # Settings validation and gates
|   |   +-- test_claim_lifecycle.py # Claim status transitions and withdrawals
|   |   +-- test_claims_read_api.py # GET /claims, GET /claims/{id} owner scoping
|   |   +-- test_conversations_api.py # Conversation list and detail API
|   |   +-- test_rag_eval.py        # RAG evaluation endpoints and dataset
|   |   +-- test_feedback_api.py    # Answer feedback API
|   |   +-- test_admin_api.py       # Role-gated admin queue, jobs, audit, feedback
|   |   +-- test_notification_outbox.py # Notification jobs, templates, and drain
|   |   +-- test_distributed_state.py # Redis-shared idempotency/rate limits + DB sessions
|   |   +-- test_observability.py   # Request ids, metrics, telemetry, detailed health
|   |   +-- test_tracing.py         # Tracing API boundary and the LangSmith implementation
|   |   +-- contract/               # Frozen HTTP contract suite
|   |   |   +-- conftest.py         # Contract harness (golden comparison)
|   |   |   +-- llm_double.py       # Deterministic LLM double for keyless runs
|   |   |   +-- golden/             # Recorded response bodies for pinned routes
|   |   |   +-- test_contract_auth.py
|   |   |   +-- test_contract_auth_modes.py
|   |   |   +-- test_contract_chat.py
|   |   |   +-- test_contract_errors.py
|   |   |   +-- test_contract_claim_atomicity.py
|   |   |   +-- test_contract_conversations_claims.py
|   |   |   +-- test_contract_rag_eval.py
|   |   +-- domain/                 # Domain-level unit tests (claims, policies, retrieval)
|   |   +-- integration/            # Database-backed integration tests
|   +-- scripts/                    # Baseline harness scripts
|       +-- baseline.sh             # Tier A/B selection for the baseline harness
|
+-- frontend/                       # React + Vite Web Application
    +-- Dockerfile                  # Multi-stage Node 20-alpine build
    +-- .dockerignore               # Frontend build context filter
    +-- package.json                # NPM packages & scripts
    +-- tsconfig.json               # TypeScript configuration
    +-- vite.config.ts              # Vite build configuration
    +-- tailwind.config.js          # Tailwind CSS styling setup
    +-- postcss.config.js           # PostCSS configuration
    +-- src/
        +-- main.tsx                # React application entry point
        +-- App.tsx                 # Router configuration (login, signup, chat)
        +-- providers.tsx           # React providers wrapper
        +-- vite-env.d.ts           # Vite type declarations
        +-- globals.css             # Global styling & scrollbars
        +-- pages/
        |   +-- RootRedirect.tsx    # Root route redirect
        |   +-- LoginPage.tsx       # User sign-in page
        |   +-- SignupPage.tsx      # User registration page
        |   +-- ChatPage.tsx        # Main chat interface
        +-- components/
        |   +-- ChatInput.tsx       # Message input box with keyboard shortcuts
        |   +-- ChatWindow.tsx      # Main conversation window with autoscroll
        |   +-- MessageBubble.tsx   # Message rendering with markdown, sources, tools
        |   +-- ClaimConfirmCard.tsx # Two-step claim confirmation card
        |   +-- Sidebar.tsx         # Conversation actions & sample prompt pills
        |   +-- SourcesBadge.tsx    # Expandable policy source citation pills
        |   +-- ToolCallBadge.tsx   # Transparent tool execution indicator pills
        |   +-- AuthModal.tsx       # Authentication modal component
        +-- lib/
        |   +-- api.ts              # Frontend client for FastAPI communication
        |   +-- validation.ts       # Form validation utilities
        +-- hooks/
        |   +-- useChatMessages.ts  # Chat message state management (streaming)
        +-- types/
        |   +-- chat.ts             # TypeScript definitions for Chat, Messages & Tools
        +-- context/
            +-- AuthContext.tsx     # Authentication state management
        +-- public/
            +-- readme/             # Screenshot images for documentation
                +-- 01-empty-state.png
                +-- 02-login.png
                +-- 03-chat-interface.png
                +-- 04-policy-question.png
                +-- 05-tool-details.png
                +-- 06-claim-status.png
```

---

## Tech Stack

| Category               | Component / Library              | Version / Model          | Role in OmniCare                                                 |
| :--------------------- | :------------------------------- | :----------------------- | :--------------------------------------------------------------- |
| **Agent Framework**    | `google-adk`                     | `^1.2.0`                 | Autonomous agent lifecycle, session handling, tool binding       |
| **LLM Gateway**        | `litellm`                        | `^1.84.0`                | Provider-agnostic routing to OpenAI, Anthropic, Gemini, etc.     |
| **Default Model**      | OpenAI GPT-4o-mini               | `openai/gpt-4o-mini`     | Reasoning, intent classification, and natural language synthesis |
| **Vector DB**          | `pgvector`                      | `PostgreSQL 16`         | Native pgvector extension for hybrid vector + PostgreSQL full-text search RAG search       |
| **Embeddings**         | OpenAI Embedding API             | `text-embedding-3-small` | Hosted embedding via OpenAI                                      |
| **Backend Framework**  | `fastapi`                        | `0.115.6`                | Asynchronous high-performance REST API                           |
| **ASGI Server**        | `uvicorn`                        | `0.34.0`                 | Production ASGI web server                                       |
| **Validation**         | `pydantic` / `pydantic-settings` | `2.7.1`                  | Robust runtime typing and environment validation                 |
| **Authentication**     | `PyJWT` + `Argon2-cffi`          | Latest                   | JWT token management and password hashing                        |
| **Database ORM**       | `sqlalchemy`                     | Latest                   | Async ORM for PostgreSQL with asyncpg                            |
| **Rate Limiting**      | `slowapi`                        | Latest                   | Production rate limiting with user/IP key functions              |
| **Frontend Framework** | `React`                          | `18.2.0`                 | Component-based interactive UI                                   |
| **Routing**            | `react-router-dom`               | Latest                   | Client-side routing for SPA                                       |
| **Build Tool**         | `Vite`                           | `^5.4.11`               | Frontend build tooling and dev server                            |
| **Styling**            | `Tailwind CSS`                   | `3.3.0`                  | Clean enterprise dark-mode design system                         |
| **Icons**              | `lucide-react`                   | `^0.300.0`               | Modern SVG iconography                                           |
| **Markdown**           | `react-markdown`                 | `^9.0.0`                 | Rich text rendering with code syntax highlighting                |
| **Notifications**      | `react-hot-toast`                | Latest                   | Toast notification system                                        |
| **Containerization**   | `Docker` & `Docker Compose`      | `v3.8+`                  | Isolated multi-container environments & orchestration            |
| **Migrations**         | `alembic`                        | Latest                   | Database schema version control and migrations                   |
| **Test Suite**         | `pytest` & `pytest-asyncio`      | `^8.3.0`                 | Unit & integration testing suite                                 |

---

## Security & Guardrails

The OmniCare Assistant adheres to strict enterprise safety controls:

1. **Authentication & Authorization**: JWT-based session management with HTTP-only cookies, Argon2id password hashing, and timing-safe password comparison.
2. **Prompt Injection Defense**: Guardrail instructions explicitly reject system prompt extraction, persona hijacking, and instruction overrides.
3. **Domain Boundary Enforcement**: Restricts responses strictly to insurance policy coverage, claims lookups, and submissions.
4. **Data Integrity**: New claim submissions enforce schema validation (positive amounts, required policy identifiers, minimum description lengths) before modifying storage.
5. **Owner-Scoped Data Access**: All database queries are scoped to the authenticated user's `user_id` to prevent horizontal privilege escalation.
6. **SQL Injection Prevention**: All user-supplied values are passed as bound parameters; SQL identifiers are validated against a strict allowlist regex.
7. **Idempotency Protection**: Optional `Idempotency-Key` header prevents duplicate LLM calls and claim submissions on retry.
8. **Rate Limiting**: 20 requests/minute for chat endpoints, 10/minute for session reset.
9. **Request Size Limits**: 5MB maximum request body size to prevent abuse.
10. **Least-Privilege Containers**: Frontend production image runs under a dedicated, unprivileged `nextjs` user.

---

## Walkthrough

### Application in Action

The OmniCare Financial prototype delivers three core capabilities through a unified chat interface:

#### 1. User Authentication

Users must authenticate before accessing the chat interface. The system supports both signup and signin with secure password hashing.

![Login Screen](frontend/public/readme/02-login.png)

**Sign-in screen** — users authenticate with email and password before accessing the chat.

![Empty Chat State](frontend/public/readme/01-empty-state.png)

**Empty state** — when no conversation is active, the chat window shows suggested prompts to help users get started.

#### 2. Policy Coverage Questions (RAG with Citations)

When a user asks about policy coverage, the agent queries the pgvector hybrid search and returns grounded answers with source citations.

**Example interaction:**

> **User:** "Does my policy cover water damage from a burst pipe, and what is the limit?"

> **Assistant:** "Yes, water damage caused by sudden pipe bursts is covered up to $25,000 with a $500 deductible under **Section 1: Home Water Damage Coverage**. Note that gradual leaks or flood damage are strictly excluded."

The response includes a clickable source badge linking back to the exact policy section.

![Policy Question with Citations](frontend/public/readme/04-policy-question.png)

**Policy coverage question** — the agent returns a grounded answer with source citations from the policy document. Citations are displayed as expandable badges under the response.

#### 3. Claim Status Lookup

The agent retrieves real-time claim information from the mock claims database.

**Example interaction:**

> **User:** "Can you check the current status of claim CLM-8821?"

> **Assistant:**
> - **Claim ID:** CLM-8821
> - **Policy Number:** POL-1092
> - **Claim Type:** Water Damage
> - **Amount:** $3,500.00
> - **Status:** Approved

A tool call badge displays the `get_claim_status` invocation transparently.

![Claim Status Lookup](frontend/public/readme/06-claim-status.png)

**Claim status lookup** — the agent queries the claims database and returns the claim status with a citation to the OmniCare claims system.

#### 4. New Claim Submission

The agent validates inputs with Pydantic and facilitates new claim submissions with a two-step confirmation flow.

**Example interaction:**

> **User:** "I want to submit a water damage claim for policy POL-1092. Amount is $5000. A pipe burst in my kitchen yesterday causing flooding."

> **Assistant:** "Your claim has been prepared. Please confirm the submission in the UI to complete your claim."

After user confirmation:

> **Assistant:** "Your claim CLM-XXXX has been successfully submitted and is now being processed. Keep this ID for your records."

![Tool Details Expanded](frontend/public/readme/05-tool-details.png)

**Tool call transparency** — clicking "Tool details" expands the underlying tool invocation, showing which tool was called and with what arguments.

#### 5. Conversation History

The sidebar maintains a persistent list of all conversations. Users can browse, select, and resume previous conversations with full message history including sources and tool call details.

![Chat Interface](frontend/public/readme/03-chat-interface.png)

**Main chat interface** — sidebar shows conversation history, and the chat window displays the current conversation with suggested prompts.

#### Safety Guardrails

The system prompt includes prompt-injection defenses. Attempts to override instructions, role-play as an administrator, or extract system instructions are explicitly detected and rejected with a domain-boundary response.

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

## Architecture Decisions

### Why Google ADK + LiteLLM?

Google ADK provides a production-grade agent runtime with native session management, tool orchestration, and streaming support. LiteLLM decouples the agent from any specific LLM provider, allowing instant model switching without code changes. Together, they provide a clean separation between agent logic and model selection.

### Why pgvector over a dedicated vector database?

pgvector extends PostgreSQL with native vector similarity search. By storing embeddings in the same database as claims and conversations, we eliminate the need for a separate vector database process, reduce operational complexity, and enable unified backup/restore. The hybrid search (vector + PostgreSQL full-text search RRF) provides robust retrieval even when one modality fails.

### Why two-step claim submission?

The two-step `prepare` → `confirm` flow prevents unauthorized or accidental claim submissions. The agent can collect and validate claim data, but the final submission requires explicit user confirmation in the UI. The `confirmation_token` is single-use and time-limited, preventing replay attacks.

### Why idempotent chat endpoint?

Network failures during chat requests can cause duplicate LLM calls, wasting tokens and producing inconsistent results. The optional `Idempotency-Key` header allows the frontend to safely retry failed requests without triggering duplicate agent execution. The cache is user-namespaced and TTL-based (5 minutes).

### Why memory sessions by default, and PostgreSQL when it matters?

The default is an in-process ADK session service: zero external dependencies, no round-trips, and on a single-process deployment nothing to gain from sharing. Session state is bounded (max 500 sessions) with FIFO eviction so memory cannot grow without limit. `SESSION_STORAGE=database` switches to the PostgreSQL-backed store (`agent_sessions` / `agent_session_events`), which is what makes a second replica able to continue a conversation and what makes a restart stop forgetting one — any request can be served by any instance, and the turns themselves are already persisted either way. Redis is not a session store here: it carries the idempotency cache and rate-limit counters, and nothing else. Note that flipping a running deployment from `memory` to `database` abandons the sessions that process was holding, once.

---

## Tech Stack Summary

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | React 18, Vite, Tailwind CSS | ChatGPT-style chat UI |
| **Routing** | React Router DOM | Client-side SPA routing |
| **Backend** | FastAPI, Uvicorn | REST API server |
| **AI Agent** | Google ADK | Agent orchestration & tool management |
| **LLM Routing** | LiteLLM | Model-agnostic LLM provider |
| **LLM** | OpenAI GPT-4o-mini | Language model (via LiteLLM) |
| **Vector DB** | pgvector (PostgreSQL) | Policy document embeddings & hybrid RAG |
| **Embeddings** | OpenAI Embedding API (text-embedding-3-small) | Hosted embedding model |
| **Authentication** | PyJWT + Argon2 | JWT tokens and password hashing |
| **Database** | PostgreSQL 16 + SQLAlchemy | Persistent data storage |
| **Migrations** | Alembic | Schema version control |
| **Validation** | Pydantic | Request/response & data validation |
| **Rate Limiting** | SlowAPI | API abuse protection |
| **Testing** | pytest | Automated test suite |
| **Deployment** | Docker, Docker Compose | Containerization |

---

### UI Screens

The React frontend provides a ChatGPT-style dark theme chat experience:

![Login Screen](frontend/public/readme/02-login.png)

**Sign-in screen** — users authenticate with email and password before accessing the chat.

![Chat Interface](frontend/public/readme/03-chat-interface.png)

**Main chat interface** — sidebar shows conversation history, and the chat window displays the current conversation with suggested prompts.

![Empty Chat State](frontend/public/readme/01-empty-state.png)

**Empty state** — when no conversation is active, the chat window shows suggested prompts to help users get started.

### UI Features

- **ChatGPT-style dark theme** chat window with auto-scroll
- **Markdown-rendered** assistant responses with syntax highlighting
- **Expandable source citation badges** showing policy section and document
- **Collapsible tool call details** showing function name, arguments, and results
- **Copy message** button for assistant responses
- **Retry** button for failed messages
- **Sidebar** with conversation history and sample prompt quick-start pills
- **Responsive design** with touch swipe sidebar and mobile menu button
- **Loading states** with spinner during auth hydration
- **Streaming responses** via Server-Sent Events

### API Verification

You can verify the system is working without the UI:

```bash
# Health check
curl http://localhost:8000/api/v1/health
# -> {"status": "healthy"}

# Interactive documentation
open http://localhost:8000/docs        # Swagger UI
open http://localhost:8000/redoc       # ReDoc
```

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes and run tests (`pytest tests/ -v`)
4. Commit your changes (`git commit -m 'Add amazing feature'`)
5. Push to the branch (`git push origin feature/amazing-feature`)
6. Open a Pull Request

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.
