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
|                                              |  * InMemorySessionService per User    |  |
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

    Docker Compose builds and starts four services: the PostgreSQL 16 (pgvector) database, the FastAPI backend, the `embedding-drain` worker (drains the embedding-job outbox), and the React/Vite frontend. The backend and worker wait for the database health check; the frontend waits for the backend health check (`/api/v1/health`) to report `healthy` before accepting traffic. The database is published on `127.0.0.1:15432` (loopback only) to avoid colliding with a local PostgreSQL on 5432.

4. **Open the Application**
    Navigate to [http://localhost:3000](http://localhost:3000) in your browser. The backend will automatically:
    - Apply pending database migrations via Alembic
    - Ingest the policy document into pgvector on startup
    - Start accepting requests

---

## Framework Selection Rationale

| Technology                               | Selection Rationale                                                                                                                                           | Key Advantages                                                                                                                                                                                                                                                                                                                                                                                                           |
| :--------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Google ADK** _(Agent Development Kit)_ | **Enterprise Agent Framework** -- Standardized, resilient agent orchestration engine providing native session tracking, tool execution loops, and guardrails. | _ **Native Tool Orchestration**: Converts standard Python functions directly into model-consumable tool definitions.<br>_ **Multi-Turn Session State**: First-class `InMemorySessionService` cleanly isolates user conversations with FIFO eviction (max 500 sessions).<br>_ **Model Agnostic**: Seamlessly interfaces with third-party providers via LiteLLM.<br>_ **Clean Pattern**: Separates system instructions, tool definitions, and runtime execution. |
| **LiteLLM**                              | **Universal LLM Proxy & Router** -- Decouples the core agent code from vendor-specific LLM APIs.                                                              | _ **100+ Provider Support**: Switch effortlessly between OpenAI, Anthropic, Google Gemini, Azure, and open-source models.<br>_ **Zero Code Changes**: Change the model with a single environment variable (`LLM_MODEL`).<br>\* **Standardized Input/Output**: Normalizes API schemas, cost tracking, and error handling across providers.                                                                                |
| **pgvector**                          | **PostgreSQL Vector Extension** -- Native pgvector extension inside PostgreSQL for hybrid vector + PostgreSQL full-text search with Reciprocal Rank Fusion. | _ **Unified Storage**: Embeddings and keyword search live in the same Postgres instance as claims and conversations.<br>_ **Hybrid Search**: Combines vector similarity (HNSW) with PostgreSQL full-text search (tsvector/tsquery) using RRF for robust retrieval.<br>_ **No External Service**: Eliminates the need for a separate vector database process or Docker volume.                                                                                   |
| **ragit** _(GitHub)_           | **Domain-Agnostic RAG Toolkit** -- Published package (pinned git dependency, `ragit[pgvector] @ v0.2.0`) carrying every generic RAG primitive: chunking, embeddings, vector stores, ingestion, evaluation, and the job outbox. | _ **Clean Separation**: OmniCare-specific bindings live in `backend/app/domain/`; generic machinery lives in `ragit`.<br>_ **Library Isolation**: ragit never imports `app.*`.<br>_ **Reusable**: Any FastAPI/SQLAlchemy service can adopt it; see [github.com/eslam-aboutaleb/ragit](https://github.com/eslam-aboutaleb/ragit). |

### The ragit Library

The RAG subsystem is extracted into an installable, domain-agnostic library so the retrieval machinery can be reused (and tested) independently of OmniCare's schema. `ragit` is published from [github.com/eslam-aboutaleb/ragit](https://github.com/eslam-aboutaleb/ragit). The backend pins it as a git dependency with the `pgvector` extra in `backend/pyproject.toml`:

```
"ragit[pgvector] @ git+https://github.com/eslam-aboutaleb/ragit.git@v0.2.0",
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

**Domain adapters** (`backend/app/domain/`) bind ragit to OmniCare's tables: `domain/policies/` owns policy versioning and ingestion, `domain/claims/` owns claim ingestion, the embedding-job outbox, and owner-scoped claim retrieval, and `domain/embeddings.py` wires the embedding function and vector store. The claims adapter always passes `owner_id` as a retrieval filter, so cross-user data leakage is structurally impossible.

**Embedding drain worker** (`embedding-drain` service): claim embeddings never block claim submission. `POST /api/v1/claims/confirm` writes the claim and its `embedding_jobs` row in the same transaction (outbox pattern); the drainer worker claims pending jobs with `FOR UPDATE SKIP LOCKED`, generates the embedding, and upserts it into pgvector. Multiple drainer replicas scale without coordination, and jobs stuck in `processing` past `JOB_STALE_AFTER_SECONDS` are reclaimed automatically.

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
| `GET` | `/api/v1/chat/conversations/{id}` | Get full message history for a conversation |

### Claims Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/claims/prepare` | Validate and create a pending claim submission |
| `POST` | `/api/v1/claims/confirm` | Confirm and submit a pending claim |

### Health & Docs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Service health check |
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

---

## Running Tests

OmniCare Financial includes automated tests with Pytest covering authentication, RAG ingestion, vector retrieval, agent tool execution, idempotency, and API endpoints. The backend suite (410 tests) runs against a dedicated test database (`OMNICARE_TEST_DATABASE_URL`) and exercises core paths and corner cases (empty inputs, invalid parameters, locking, stale-job reclaim, versioning, and cross-user isolation). The generic RAG machinery is covered by the ragit package's own test suite, published from [github.com/eslam-aboutaleb/ragit](https://github.com/eslam-aboutaleb/ragit).

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

The backend suite maintains **98% line coverage** of `app/` (target: >90%).

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
| `tests/test_rag.py` | Policy retrieval, hybrid search, RRF scoring |
| `tests/test_pgvector_store.py` | pgvector hybrid search, upsert, count operations |
| `tests/test_claim_status.py` | Claim status lookup, owner scoping |
| `tests/test_submit_claim.py` | Claim preparation and confirmation flow |
| `tests/test_search_claims.py` | Natural language claims search |
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
|   +-- pyproject.toml              # Build tool specifications
|   +-- uv.lock                     # Locked dependencies (incl. ragit git pin)
|   +-- alembic/                    # Database migration version control
|   |   +-- env.py                  # Alembic environment configuration
|   |   +-- script.py.mako          # Migration template
|   |   +-- versions/               # Migration scripts
|   |       +-- 6f253a5df2e9_initial_schema.py
|   |       +-- a1b2c3d4e5f6_add_pgvector_embeddings.py
|   |       +-- 822df1979a12_add_conversation_messages_claim_.py
|   +-- app/
|   |   +-- __init__.py             # Package init
|   |   +-- config.py               # Pydantic Settings configuration loader
|   |   +-- main.py                 # FastAPI application, CORS, lifespan hooks
|   |   +-- database.py             # SQLAlchemy async engine & session factory
|   |   +-- auth.py                 # JWT auth, Argon2 hashing, cookie management
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
|   |   |       +-- claims.py       # POST /claims/prepare, /claims/confirm
|   |   |       +-- rag_eval.py     # POST /rag/evaluate, GET /rag/dataset
|   |   +-- domain/                 # Domain adapters over ragit (plan 06)
|   |   |   +-- __init__.py         # Domain package init
|   |   |   +-- embeddings.py       # EmbeddingFactory + vector store wiring
|   |   |   +-- evaluation.py       # RAG evaluation CLI runner
|   |   |   +-- claims/             # Claims domain bindings
|   |   |   |   +-- __init__.py     # Claims package init
|   |   |   |   +-- ingest.py       # Claim ingestion into the claims vector store
|   |   |   |   +-- outbox.py       # Embedding-job outbox (SQLAlchemy store + processor)
|   |   |   |   +-- retriever.py    # Claims hybrid retrieval (owner-scoped)
|   |   |   |   +-- tools.py        # search_claims tool binding
|   |   |   +-- policies/           # Policies domain bindings
|   |   |       +-- __init__.py     # Policies package init
|   |   |       +-- ingestion.py    # Policy versioning + ingestion pipeline
|   |   |       +-- retriever.py    # Policy hybrid retrieval
|   |   |       +-- tools.py        # query_policy tool binding
|   |   +-- data/                   # Policy documents
|   |   |   +-- sample_policy.md    # Source policy document for RAG
|   |   +-- models/                 # SQLAlchemy ORM models
|   |   |   +-- __init__.py         # Models package init
|   |   |   +-- base.py             # Declarative base with common columns
|   |   |   +-- user.py             # User model (Argon2 password hashes)
|   |   |   +-- claim.py            # Claim model
|   |   |   +-- claim_submission.py # ClaimSubmission model (pending claims)
|   |   |   +-- conversation.py     # Conversation model (JSONB messages)
|   |   |   +-- conversation_message.py # ConversationMessage model (normalized)
|   |   |   +-- policy_chunk.py     # PolicyChunk model
|   |   |   +-- policy_version.py   # PolicyVersion model (ingestion snapshots)
|   |   |   +-- policy_ingestion_meta.py # Per-source ingestion metadata
|   |   |   +-- embedding_job.py    # EmbeddingJob model
|   |   +-- schemas/                # Pydantic data schemas
|   |   |   +-- __init__.py         # Schemas package init
|   |   |   +-- models.py           # Request, response, and claim validation models
|   |   +-- workers/                # Background workers
|   |       +-- embedding_drain.py  # Outbox drainer (FOR UPDATE SKIP LOCKED)
|   +-- tests/
|       +-- conftest.py             # Shared fixtures
|       +-- test_health.py          # Health endpoint tests
|       +-- test_auth_security.py   # Authentication & security tests
|       +-- test_chat.py            # Chat endpoint tests
|       +-- test_chat_stream.py     # Streaming chat endpoint tests
|       +-- test_idempotency.py     # Idempotency tests
|       +-- test_rag.py             # RAG retrieval tests
|       +-- test_pgvector_store.py  # pgvector store tests
|       +-- test_claim_status.py    # Claim lookup tests
|       +-- test_submit_claim.py    # Claim submission tests
|       +-- test_search_claims.py   # Claims search tests
|       +-- test_conversation_store.py # Conversation persistence tests
|       +-- test_security_isolation.py # Cross-user isolation tests
|       +-- test_llm_e2e.py         # End-to-end LLM integration tests
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
        +-- app/
        |   +-- globals.css         # Global styling & scrollbars
        +-- pages/
        |   +-- RootRedirect.tsx    # Root route redirect
        |   +-- LoginPage.tsx       # User sign-in page
        |   +-- SignupPage.tsx      # User registration page
        |   +-- ChatPage.tsx        # Main chat interface
        +-- components/
        |   +-- ChatInput.tsx       # Message input box with keyboard shortcuts
        |   +-- ChatWindow.tsx      # Main conversation window with autoscroll
        |   +-- MessageBubble.tsx   # Message rendering with markdown, sources, tools
        |   +-- ChatMessageList.tsx # Scrollable message list container
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
| **Build Tool**         | `Vite`                           | `^8.3.1`                 | Frontend build tooling and dev server                            |
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

### Why InMemorySessionService?

For a prototype and moderate production loads, the `InMemorySessionService` provides excellent performance with zero external dependencies. Session state is bounded (max 500 sessions) with FIFO eviction to prevent unbounded memory growth. For multi-instance deployments, a Redis-backed session service can be swapped in with minimal code changes.

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
