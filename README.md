# OmniCare Financial - Customer Assistant Prototype

A full-stack AI-powered customer assistant for **OmniCare Financial**, an enterprise insurance company. Policyholders can authenticate, ask policy coverage questions (with citations), look up claim statuses, and submit new claims - all through a modern chat interface.

> **Note:** OmniCare Financial is a fictional company. This repository is an engineering showcase that ships with synthetic sample policy and claim data (`backend/app/data/`) generated for demonstration purposes; it is not affiliated with, endorsed by, or derived from any real insurance company or client engagement.

Powered by **Google Agent Development Kit (ADK)** and **LiteLLM**, the assistant autonomously resolves policy coverage inquiries using local **pgvector** vector retrieval (RAG), checks live claim status records, and facilitates new claim submissions with full schema validation and audit trails.

---

## Documentation

Start with the quick start below. The full engineering reference is split into focused pages:

- [Architecture and data flow](docs/architecture.md)
- [API endpoints and cURL walkthroughs](docs/api.md)
- [Environment variables and configuration](docs/configuration.md)
- [Deployment, design decisions and technology stack](docs/operations.md)

**CI quality gates:** [RAG Eval Gate](https://github.com/eslam-aboutaleb/insurance-claims-rag-assistant/actions/workflows/rag-eval.yml) enforces retrieval and refusal floors on an indexed synthetic corpus. [Full Suite CI](https://github.com/eslam-aboutaleb/insurance-claims-rag-assistant/actions/workflows/ci.yml) runs all backend tests plus frontend lint, typecheck, tests and build independently.

---

## Quick Start (2 Minutes)

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- An [OpenAI API key](https://platform.openai.com/api-keys) (free tier is sufficient)

### Steps

1. **Clone the repository**

    ```bash
    git clone https://github.com/eslam-aboutaleb/insurance-claims-rag-assistant.git
    cd insurance-claims-rag-assistant
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

## Demo and source documentation

The [architecture reference](docs/architecture.md) explains the request and retrieval paths, while the [API guide](docs/api.md) includes examples for policy questions, claims lookup, and confirmation-gated submission.

The screenshot assets and walkthrough remain available under `frontend/public/readme/`.

---

## Licensing

All rights reserved. No license is currently granted to use, modify, or redistribute this code.

