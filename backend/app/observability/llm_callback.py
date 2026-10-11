"""LiteLLM callback that records tokens, cost, and latency for every model call.

LiteLLM runs its logging hooks on the same completion path as the chat and
streaming endpoints, which makes this the one place that sees every billable
call without touching either endpoint. Everything here is defensive on purpose:
the hooks are invoked from LiteLLM's own error handling, so raising out of them
would turn an already failed model call into a failed request. Telemetry is
best-effort -- a chat request never fails because of it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger

from app.observability.metrics import build_llm_call, get_metrics

logger = logging.getLogger(__name__)

_UNKNOWN_MODEL = "unknown"
_UNKNOWN_ERROR = "unknown error"


def _lookup(source: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping, a LiteLLM response object, or a model.

    LiteLLM hands the hooks the logging payload as a plain dict and the response
    as a pydantic object, so the same field has to be readable from either. A
    ``None`` result is normalised to ``default`` because LiteLLM leaves optional
    response fields absent rather than populated.

    Args:
        source: Object, mapping, or pydantic model to read from. ``None`` is
            accepted and yields ``default``.
        key: Field name to read.
        default: Value returned when the field is missing or ``None``.

    Returns:
        Any: The field value, or ``default``.
    """
    if source is None:
        return default
    if isinstance(source, Mapping):
        value = source.get(key)
    else:
        value = getattr(source, key, None)
        if value is None:
            getter = getattr(source, "get", None)
            value = getter(key) if callable(getter) else None
    return default if value is None else value


def _as_float(value: Any) -> float | None:
    """Coerce a numeric ``value`` to float, returning ``None`` for anything else."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _resolve_tokens(standard: Any, usage: Any, key: str) -> int:
    """Return a token count from the logging payload or the response usage.

    Args:
        standard: LiteLLM's ``standard_logging_object`` payload, when present.
        usage: Token usage object from the response, when present.
        key: Either ``"prompt_tokens"`` or ``"completion_tokens"``.

    Returns:
        int: The token count, or 0 when no source reports one -- cache hits and
        embeddings legitimately have none.
    """
    value = _lookup(standard, key)
    if value is None:
        value = _lookup(usage, key)
    parsed = _as_float(value)
    return int(parsed) if parsed is not None else 0


def _resolve_cost(kwargs: Any, response_obj: Any, standard: Any) -> float:
    """Price one call, preferring LiteLLM's own calculator.

    ``litellm.completion_cost`` is authoritative, but it raises for models that
    are absent from its price map and for responses it cannot read. In both
    cases the cost LiteLLM already stamped on the logging payload is the next
    best thing, because a call still costs *something*.

    Args:
        kwargs: The logging payload LiteLLM passed to the hook.
        response_obj: The provider response, if the call produced one.
        standard: LiteLLM's ``standard_logging_object``, when present.

    Returns:
        float: Cost in USD; 0.0 when no source could price the call.
    """
    if response_obj is not None:
        try:
            return float(litellm.completion_cost(completion_response=response_obj))
        except Exception:
            logger.debug("litellm.completion_cost could not price this call", exc_info=True)
    for source in (standard, kwargs):
        cost = _as_float(_lookup(source, "response_cost"))
        if cost is not None:
            return cost
    return 0.0


def _resolve_model(kwargs: Any, response_obj: Any, standard: Any) -> str:
    """Return the model name from the first source that reports one.

    Args:
        kwargs: The logging payload LiteLLM passed to the hook.
        response_obj: The provider response, if the call produced one.
        standard: LiteLLM's ``standard_logging_object``, when present.

    Returns:
        str: The model identifier, or ``"unknown"``.
    """
    for source in (standard, kwargs, response_obj):
        model = _lookup(source, "model")
        if isinstance(model, str) and model:
            return model
    return _UNKNOWN_MODEL


def _elapsed_seconds(start_time: Any, end_time: Any) -> float | None:
    """Return the seconds between two timestamps given as datetimes or numbers.

    Args:
        start_time: When the call began, as reported by LiteLLM.
        end_time: When the call ended, as reported by LiteLLM.

    Returns:
        float | None: Elapsed seconds, or ``None`` when the timestamps cannot be
        subtracted -- mixing a naive with an aware datetime raises, and which of
        the two LiteLLM reports depends on the provider.
    """
    if isinstance(start_time, datetime) and isinstance(end_time, datetime):
        try:
            return max(0.0, (end_time - start_time).total_seconds())
        except TypeError:
            return None
    start = _as_float(start_time)
    end = _as_float(end_time)
    if start is None or end is None:
        return None
    return max(0.0, end - start)


def _resolve_latency(start_time: Any, end_time: Any, standard: Any) -> float:
    """Return the call duration in seconds.

    Args:
        start_time: When the call began, as reported by LiteLLM.
        end_time: When the call ended, as reported by LiteLLM.
        standard: LiteLLM's ``standard_logging_object``, which already measures
            the round trip and is the fallback for cache hits and providers that
            call the hook without timing information.

    Returns:
        float: Duration in seconds, never negative.
    """
    latency = _elapsed_seconds(start_time, end_time)
    if latency is not None:
        return latency
    return _as_float(_lookup(standard, "response_time")) or 0.0


def _resolve_error(kwargs: Any) -> str:
    """Describe why a call failed, for the failed-call record.

    Args:
        kwargs: The logging payload LiteLLM passed to the hook.

    Returns:
        str: The exception LiteLLM captured, or its message, or a placeholder.
    """
    exception = _lookup(kwargs, "exception") or _lookup(kwargs, "original_exception")
    if exception is not None:
        return exception if isinstance(exception, str) else repr(exception)
    return _lookup(kwargs, "error_information") or _UNKNOWN_ERROR


class LlmCostCallback(CustomLogger):
    """``CustomLogger`` that appends one :class:`LlmCall` per model completion.

    Both the synchronous and the asynchronous hooks are implemented because
    LiteLLM routes a completion through whichever pair matches the caller, and
    the agent drives LiteLLM asynchronously.
    """

    def log_success_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """Record a successful synchronous completion.

        Args:
            kwargs: LiteLLM logging payload.
            response_obj: The provider response.
            start_time: Call start reported by LiteLLM.
            end_time: Call end reported by LiteLLM.
        """
        self._record(kwargs, response_obj, start_time, end_time, success=True)

    def log_failure_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """Record a failed synchronous completion.

        Args:
            kwargs: LiteLLM logging payload.
            response_obj: The provider response, when one was produced.
            start_time: Call start reported by LiteLLM.
            end_time: Call end reported by LiteLLM.
        """
        self._record(kwargs, response_obj, start_time, end_time, success=False)

    async def async_log_success_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """Record a successful asynchronous completion.

        Args:
            kwargs: LiteLLM logging payload.
            response_obj: The provider response.
            start_time: Call start reported by LiteLLM.
            end_time: Call end reported by LiteLLM.
        """
        self._record(kwargs, response_obj, start_time, end_time, success=True)

    async def async_log_failure_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """Record a failed asynchronous completion.

        Args:
            kwargs: LiteLLM logging payload.
            response_obj: The provider response, when one was produced.
            start_time: Call start reported by LiteLLM.
            end_time: Call end reported by LiteLLM.
        """
        self._record(kwargs, response_obj, start_time, end_time, success=False)

    def _record(
        self,
        kwargs: Any,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        *,
        success: bool,
    ) -> None:
        """Write one completion event, swallowing every failure.

        Args:
            kwargs: LiteLLM logging payload.
            response_obj: The provider response, when one was produced.
            start_time: Call start reported by LiteLLM.
            end_time: Call end reported by LiteLLM.
            success: Whether the call returned a response.
        """
        try:
            standard = _lookup(kwargs, "standard_logging_object")
            usage = _lookup(response_obj, "usage")
            get_metrics().record_llm_call(
                build_llm_call(
                    model=_resolve_model(kwargs, response_obj, standard),
                    prompt_tokens=_resolve_tokens(standard, usage, "prompt_tokens"),
                    completion_tokens=_resolve_tokens(standard, usage, "completion_tokens"),
                    cost_usd=_resolve_cost(kwargs, response_obj, standard),
                    latency_seconds=_resolve_latency(start_time, end_time, standard),
                    success=success,
                    error="" if success else _resolve_error(kwargs),
                )
            )
        except Exception:
            logger.debug("LLM telemetry dropped", exc_info=True)


def register_llm_cost_callback() -> LlmCostCallback:
    """Add the cost callback to LiteLLM's process-wide callback list.

    ``litellm.callbacks`` is copied per call by LiteLLM's own request setup, so
    appending here takes effect without touching a request path. The membership
    check keeps repeated startups -- the test suite re-runs the application
    lifespan -- from stacking duplicate callbacks that would double every count.

    Returns:
        LlmCostCallback: The active cost callback, newly registered or already
        present.
    """
    for callback in litellm.callbacks:
        if isinstance(callback, LlmCostCallback):
            return callback

    callback = LlmCostCallback()
    litellm.callbacks = [*litellm.callbacks, callback]
    logger.info("LiteLLM cost and latency callback registered")
    return callback
