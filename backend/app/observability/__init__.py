"""Observability primitives: correlation ids, structured logs, and telemetry.

Submodules are imported directly by their consumers so that importing this
package stays cheap. ``app.main`` takes the logging and request plumbing,
``app.rate_limiter`` only needs the metrics store, and the LiteLLM callback is
loaded once lifespan confirms cost tracking is enabled.
"""

from app.observability.metrics import (
    DEFAULT_WINDOW_SECONDS,
    LlmCall,
    LimiterRejection,
    MetricsRegistry,
    ToolCall,
    get_metrics,
    reset_metrics,
)
from app.observability.request_context import (
    REQUEST_ID_HEADER,
    display_request_id,
    generate_request_id,
    get_request_id,
    reset_request_id,
    resolve_request_id,
    set_request_id,
)

__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "REQUEST_ID_HEADER",
    "LlmCall",
    "LimiterRejection",
    "MetricsRegistry",
    "ToolCall",
    "display_request_id",
    "generate_request_id",
    "get_metrics",
    "get_request_id",
    "reset_metrics",
    "reset_request_id",
    "resolve_request_id",
    "set_request_id",
]
