# OmniCare Financial -- Backend Engineering Guide & Technical Architecture

Welcome to the backend service of **OmniCare Financial**, an enterprise-grade AI customer service platform. The backend provides an intelligent conversational assistant capable of answering complex policy coverage inquiries via Retrieval-Augmented Generation (RAG), querying existing insurance claim statuses, and validating/filing new claims.

---

## 1. System Architecture Overview

The backend is built with Python 3.11+ using an asynchronous, modular architecture powered by **FastAPI**, **Google ADK (Agent Development Kit)**, **LiteLLM**, and **pgvector**.

### High-Level Architectural Layers

```mermaid
graph TD
    Client["Client Layer<br/>(React Frontend / API Consumers)"]

    subgraph FastAPI_Service["FastAPI Application (Port 8000)"]
        CORS["CORS Middleware<br/>(Dynamic Origin Whitelist)"]
        Lifespan["Application Lifespan<br/>(pgvector Pre-Indexing & Health Checks)"]
        Router["API Router (/api/v1)"]

        subgraph Endpoints["v1 Endpoints"]
            HealthEP["GET /health<br/>(Liveness & Dependency Probe)"]
            ChatEP["POST /chat<br/>(Session & Agent Execution)"]
        end

        Config["Configuration Management<br/>(Pydantic v2 Settings)"]
    end

    subgraph Agent_Core["Agent Reasoning Core (Google ADK)"]
        AgentRunner["Runner & InMemorySessionService"]
        LlmAgent["LlmAgent: omnicare_assistant"]
        LiteLLM["LiteLLM Model Router"]
        SystemPrompt["Safety & System Instructions"]
    end

    subgraph Tools_Layer["Agent Tool Registry"]
        ToolRAG["query_policy<br/>(pgvector Hybrid Search)"]
        ToolStatus["get_claim_status<br/>(Claims Lookup)"]
        ToolSubmit["submit_claim<br/>(Pydantic Validation & Persistence)"]
    end

    subgraph Storage_Layer["Datastores & External Services"]
        PgVectorStore[("pgvector / PostgreSQL<br/>Hybrid Vector + PostgreSQL Full-Text Search")]
        PolicyDoc[("Source Policy<br/>sample_policy.md")]
        LLMProvider[("LLM Provider<br/>OpenAI / Anthropic / Gemini")]
    end

    Client -->|HTTP / JSON| CORS
    CORS --> Router
    Router --> HealthEP
    Router --> ChatEP

    ChatEP -->|Invoke run_agent| AgentRunner
    AgentRunner --> LlmAgent
    LlmAgent -->|Inference Request| LiteLLM
    LiteLLM -->|API Call| LLMProvider

    LlmAgent -.->|Tool Call| ToolRAG
    LlmAgent -.->|Tool Call| ToolStatus
    LlmAgent -.->|Tool Call| ToolSubmit

    ToolRAG --> PgVectorStore
    ToolStatus --> ClaimsDB
    ToolSubmit --> ClaimsDB
    Lifespan -.->|Chunk & Ingest| PolicyDoc
    Lifespan -.->|Persist Embeddings| PgVectorStore
```

### Component Responsibilities

| Component                   | Path                                                                                                               | Responsibility                                                                                                                      |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Application Entrypoint**  | [`app/main.py`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/main.py)                     | Initializes FastAPI, mounts CORS middleware, executes startup RAG ingestion, and binds API routes.                                  |
| **Configuration Core**      | [`app/core/config.py`](file:///Users/eslamaboutaleb/Documents/insurance-claims-rag-assistant/backend/app/core/config.py)                 | Centralized, type-safe environment configuration powered by Pydantic v2 `SettingsConfigDict` and `@lru_cache` dependency injection. |
| **API Endpoints**           | [`app/api/v1/`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/api/v1)                      | Declares versioned HTTP endpoints (`/health`, `/chat`) with strict semantic status codes and OpenAPI schema documentation.          |
| **Data Schemas**            | [`app/schemas/models.py`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/schemas/models.py) | Pydantic request, response, error envelopes, and tool data models.                                                                  |
| **Agent Core**              | [`app/agent/agent.py`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/agent/agent.py)       | Sets up Google ADK `LlmAgent`, `Runner`, session handling, event streaming, and tool resolution.                                    |
| **System Prompts & Safety** | [`app/agent/prompts.py`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/agent/prompts.py)   | System instructions, tool usage rules, grounding requirements, and prompt-injection defense mechanisms.                             |
| **RAG Engine**              | [`app/rag/`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/rag)                            | Markdown document chunking (`ingest.py`), LiteLLM embeddings (`LitellmEmbeddingFunction`), and hybrid vector retrieval (`retriever.py`).        |
| **Agent Tools**             | [`app/agent/tools/`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/agent/tools)            | Domain functions executable by the agent: policy lookup, claim status retrieval, and validated claim filing.                        |

---

## 2. Request Lifecycle & Logic Flow

### 2.1 Chat Processing & Agent Tool Execution

When a user submits a query to `/api/v1/chat`, the system coordinates session recovery, LLM reasoning, iterative tool execution, and source attribution:

```mermaid
sequenceDiagram
    autonumber
    actor User as Frontend Client
    participant API as FastAPI (/api/v1/chat)
    participant Runner as ADK Runner & Session
    participant Agent as ADK LlmAgent
    participant LiteLLM as LiteLLM Router
    participant LLM as Upstream LLM (GPT-4o-mini)
    participant Tools as Agent Tools (RAG / Claims)

    User->>API: POST /api/v1/chat {user_id, message}
    API->>Runner: run_agent(user_id, message)
    Runner->>Runner: _ensure_session(user_id)
    Runner->>Agent: Stream events with User Message

    Agent->>LiteLLM: Prompt + System Instruction + Available Tools
    LiteLLM->>LLM: Inference Request
    LLM-->>LiteLLM: Function Call Request (e.g., query_policy)
    LiteLLM-->>Agent: Function Call Event

    Agent->>Tools: Execute query_policy(query)
    Tools-->>Agent: Relevant chunks + Section metadata

    Agent->>LiteLLM: Function Response (Tool Results)
    LiteLLM->>LLM: Synthesize final answer with grounding
    LLM-->>LiteLLM: Grounded answer citing policy sections
    LiteLLM-->>Agent: Final Text Response Event

    Agent-->>Runner: Complete event stream
    Runner-->>API: {response, sources, tool_calls}
    API-->>User: HTTP 200 OK (ChatResponse JSON)
```

---

## 3. API Integrity & HTTP Status Code Matrix

The API strictly adheres to RESTful semantics and RFC 9110 standards. All responses return predictable, deterministic HTTP status codes:

### HTTP Status Code Reference

| Status Code                     | Description                 | Scenario / Trigger                                                                                                                                                                      |
| | ------------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`200 OK`**                    | Request succeeded           | - `GET /api/v1/health`: System is healthy and datastores are reachable.<br/>- `POST /api/v1/chat`: Message processed successfully.                                                      |
| **`400 Bad Request`**           | Malformed HTTP request      | - Invalid JSON syntax in request body.<br/>- Unsupported headers or content types.                                                                                                      |
| **`405 Method Not Allowed`**    | Verb not permitted          | - `POST /api/v1/health`: Health endpoint only permits `GET`.                                                                                                                            |
| **`422 Unprocessable Content`** | Pydantic validation failure | - Missing `user_id` or `message` in `ChatRequest`.<br/>- Empty `user_id` (`min_length=1`).<br/>- Invalid types (e.g. non-string `user_id`).                                             |
| **`500 Internal Server Error`** | Uncaught server failure     | - Agent runner crash or unhandled runtime exceptions.<br/>- **Security**: In production (`ENVIRONMENT=production`), error messages are sanitized to prevent sensitive internal leakage. |
| **`503 Service Unavailable`**   | Infrastructure failure      | - `GET /api/v1/health`: Filesystem datastore or vector database is inaccessible or permissions fail.                                                                                    |

### Standardized Error Envelope

All error responses (including validation failures) use a consistent `ErrorResponse` schema:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed. See details for field-level errors.",
    "details": [
      {
        "type": "missing",
        "loc": ["body", "user_id"],
        "msg": "Field required",
        "input": {"message": "Can I check my claim status?"}
      }
    ]
  }
}
```

This envelope is enforced globally via a `RequestValidationError` exception handler in `app/main.py`, ensuring the actual response schema always matches the OpenAPI declaration.

---

### Endpoint Reference

#### `GET /api/v1/health`

Evaluates application liveness and verifies datastore accessibility.

- **Status Codes**: `200 OK` (Healthy), `503 Service Unavailable` (Degraded)
- **Response Example**:
  ```json
  {
    "status": "healthy"
  }
  ```

#### `POST /api/v1/chat`

Processes conversational queries through the OmniCare agent.

- **Request Headers**: `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "user_id": "usr_1024",
    "message": "Is water damage from a burst pipe covered under my policy?"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "response": "Yes, water damage caused by sudden and accidental pipe bursts is covered up to $25,000 under Section 1 with a $500 deductible.",
    "sources": ["Section 1: Home Water Damage Coverage (sample_policy.md)"],
    "tool_calls": [
      {
        "name": "query_policy",
        "arguments": {
          "query": "water damage burst pipe"
        },
        "result": {
          "sources": [
            {
              "section": "Section 1: Home Water Damage Coverage",
              "source": "sample_policy.md"
            }
          ]
        }
      }
    ]
  }
  ```
- **Error Response (`422 Unprocessable Content`)**:
  ```json
  {
    "error": {
      "code": "VALIDATION_ERROR",
      "message": "Request validation failed. See details for field-level errors.",
      "details": [
        {
          "type": "missing",
          "loc": ["body", "user_id"],
          "msg": "Field required",
          "input": {"message": "Can I check my claim status?"}
        }
      ]
    }
  }
  ```

---

## 4. Configuration Management

Configuration is strictly centralized in [`app/core/config.py`](file:///Users/eslamaboutaleb/Documents/insurance-claims-rag-assistant/backend/app/core/config.py) using **Pydantic v2 `BaseSettings`** and **`SettingsConfigDict`**. No raw `os.getenv` calls are permitted across application modules.

### Environment Variables Matrix

| Variable                 | Type        | Default Value                    | Validation / Format                                    | Description                                                   |
| ------------------------ | ----------- | -------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------- |
| `ENVIRONMENT`            | `str`       | `development`                    | One of: `development`, `staging`, `production`, `test` | Runtime environment tier. Controls error detail sanitization. |
| `APP_NAME`               | `str`       | `OmniCare Financial API`         | Non-empty string                                       | Application title reflected in OpenAPI docs.                  |
| `APP_VERSION`            | `str`       | `1.0.0`                          | Semantic version string                                | Service version.                                              |
| `LOG_LEVEL`              | `str`       | `INFO`                           | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`        | Root logging level.                                           |
| `HOST`                   | `str`       | `0.0.0.0`                        | Valid IP / hostname                                    | Interface to bind Uvicorn.                                    |
| `PORT`                   | `int`       | `8000`                           | Integer `1` to `65535`                                 | Server TCP port.                                              |
| `CORS_ORIGINS`           | `list[str]` | `["http://localhost:3000", ...]` | Comma-separated string, JSON list, or list             | Whitelisted CORS origins for web clients.                     |
| `OPENAI_API_KEY`         | `str`       | `""`                             | Valid secret key string                                | OpenAI API key utilized by LiteLLM.                           |
| `OPENAI_API_BASE`        | `str`       | `""`                             | Valid URL or empty string                              | Optional OpenAI API base URL override for LiteLLM.            |
| `LLM_MODEL`              | `str`       | `openai/gpt-4o-mini`             | `provider/model_name` string                           | Model routed via LiteLLM.                                     |
| `EMBEDDING_MODEL`        | `str`       | `text-embedding-3-small`          | Pretrained model identifier                            | Embedding model name used by LiteLLM for pgvector.            |
| `INGEST_ON_STARTUP`      | `bool`      | `true`                            | `true` / `false`                                       | Index policy documents during API startup.                    |
| `EMBEDDING_DRAIN_INTERVAL_SECONDS` | `float` | `5.0`                     | Greater than `0.1`                                     | Idle sleep of the embedding drainer.                          |
| `EMBEDDING_DRAIN_BATCH_SIZE` | `int`   | `10`                              | Greater than `0`                                       | Jobs claimed per embedding drainer pass.                      |
| `POLICY_FILE_PATH`       | `str`       | `./app/data/sample_policy.md`    | Valid file path                                        | Source policy document for RAG ingestion.                     |

### Embedding dimension

The vector dimension is owned by [`app/rag/embedding_dimensions.py`](app/rag/embedding_dimensions.py). Call `get_embedding_dimension()` rather than hardcoding `1536`; it resolves the dimension for the configured `EMBEDDING_MODEL` and is the single source of truth used by ingestion, retrieval, and the vector stores. Vectors are checked by `pgvector_store._validate_embedding`, which every write and query path must call.

The two pgvector columns are still declared with a literal `Vector(1536)` in `app/models/claims/claim.py` and `app/models/policies/policy_chunk.py`. Widening or narrowing the stored column requires an `ALTER TABLE` migration and is not covered by the runtime check, so treat a change of `EMBEDDING_MODEL` as a schema migration, not a configuration change.

FastAPI endpoints consume settings through `Depends(get_settings)`:

```python
from fastapi import APIRouter, Depends
from app.core.config import Settings, get_settings

router = APIRouter()


@router.get("/example")
async def example_route(settings: Settings = Depends(get_settings)):
    return {"environment": settings.environment}
```

This enables seamless mock injection during unit and integration testing without monkeypatching environment variables.

---

## 5. Development & Testing Guide

### Prerequisites

- Python 3.11 or higher
- C/C++ compiler toolchain (for native Python package builds)
- (Optional) Docker & Docker Compose

### Local Environment Setup

1. **Navigate to the Backend Directory**:

   ```bash
   cd backend
   ```

2. **Create and Activate a Virtual Environment**:

   ```bash
   uv venv .venv
   source .venv/bin/activate
   ```

3. **Install Dependencies**:

   ```bash
   uv pip install -e .
   ```

4. **Configure Environment Variables**:

   ```bash
   cp ../.env.example .env
   # Add your OPENAI_API_KEY in .env
   ```

5. **Run the Policy Ingestion Pipeline** (Pre-computes embeddings):

   ```bash
   python -m app.rag.ingest
   ```

6. **Start the Uvicorn Development Server**:

   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

7. **Access Interactive API Documentation**:
   - Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
   - ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

### Automated Testing

The backend includes a comprehensive test suite covering API endpoints, agent tools, configuration management, and RAG retrieval:

```bash
# Run all tests
pytest tests -v

# Run specific test modules
pytest tests/test_chat.py -v
pytest tests/test_config.py -v
pytest tests/test_health.py -v
pytest tests/test_rag.py -v
pytest tests/test_submit_claim.py -v
pytest tests/test_claim_status.py -v
```

#### Test Architecture & Isolation

- **Mock LLM Execution**: `test_chat.py` mocks `run_agent` to ensure tests execute offline, deterministically, and with zero external API costs.
- **In-Memory Fixture**: Retrieval tests utilize an isolated, temporary Postgres persistent directory.

---

## 6. Production Hardening & Security Standards

1. **Input Sanitization & Validation**:
   - All payloads are strictly validated using Pydantic models before touching agent reasoning or business logic.
   - String fields strip extraneous whitespace and enforce minimum length boundaries.

2. **Error Information Leakage Prevention**:
   - In production (`ENVIRONMENT=production`), unhandled 500 exceptions return generic, user-safe error messages while logging the complete traceback with context internally.

3. **Prompt Injection & Model Guardrails**:
   - The system prompt in [`app/agent/prompts.py`](file:///Users/eslamaboutaleb/Documents/omnicare-financial/backend/app/agent/prompts.py) defines strict role boundaries. Attempts to override instructions, role-play as administrator, or extract system instructions are explicitly detected and rejected.
   - Grounding rules require that all policy coverage answers cite retrieved sections directly from `query_policy`.

4. **Container Security**:
   - The [`Dockerfile`](Dockerfile) builds upon `python:3.11-slim`, executes build-time RAG indexing, and exposes only the required port 8000.

---

## 7. Known Gaps

Deliberately recorded so the next reader does not rediscover them. None of these are
fixed by the current work; each names what closing it would involve.

### A. pgvector column dimensions are still literals

`app/models/claims/claim.py` and `app/models/policies/policy_chunk.py` declare `Vector(1536)` directly
instead of deriving it from `app/rag/embedding_dimensions.py`. The runtime dimension is
now checked centrally, but changing `EMBEDDING_MODEL` to a model of a different width
still requires an `ALTER TABLE` migration for both tables. Until that migration exists,
the two values must be changed together by hand.

### B. `/api/v1/chat` accepts an empty message

`ChatRequest.message` has no minimum length, so `POST /api/v1/chat` with `{"message": ""}`
returns 200 and runs the agent. `POST /api/v1/chat/stream` rejects the same payload
with 422 through an explicit guard. The asymmetry is frozen by the contract suite
(`test_contract_chat.py::test_chat_accepts_empty_message`). Closing it means adding
`min_length` to `ChatRequest` and re-recording `chat_response_empty_message.json`.

### C. Sign-out does not revoke an access token

`POST /api/v1/auth/logout` clears the session cookie only. The JWT is stateless and
remains valid for its full one-week lifetime if captured. Revocation needs either a
server-side deny list keyed on `jti` or short-lived tokens with refresh; the current
token has no `jti` claim to key on. Frozen by
`test_contract_auth_modes.py::test_signout_only_clears_the_cookie`.

### D. `tests/llm_e2e` depends on the live OpenAI API

`tests/test_llm_e2e.py` is gated on `OPENAI_API_KEY` and asserts on free-form model
output, so it fails intermittently and is slow. It is excluded from the frozen contract,
which uses `tests/contract/llm_double.py` instead. Treat it as a manual smoke suite, not
as a CI gate.

### E. The embedding outbox has no dead-letter alerting

Jobs exhausted after `MAX_ATTEMPTS` land in `dead_letter` with the error recorded in
`status_detail`. Nothing surfaces that state: there is no metric, no log alert, and no
endpoint. An operator finds out by querying the table.
