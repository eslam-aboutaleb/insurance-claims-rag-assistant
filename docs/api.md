# API Reference and Example Requests

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

