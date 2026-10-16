# Configuration and Environment Variables

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

