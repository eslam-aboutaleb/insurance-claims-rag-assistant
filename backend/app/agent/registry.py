"""Tool registry for the OmniCare agent.

The registry is the single place the agent's toolset is

The registry is the single place the agent's toolset is
declared: adding a tool is one ``register`` call, with no
``agent.py`` edit. The agent assembly
(:func:`app.agent.agent.create_agent`) consumes the registry
through :meth:`ToolRegistry.build_tool_list`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools.claim_status import get_claim_status
from app.agent.tools.submit_claim import prepare_claim_submission
from app.domain.claims.tools import search_claims
from app.domain.policies.tools import query_policy


@dataclass
class ToolSpec:
    """A single agent tool declaration.

    Attributes:
        name: Tool name the model invokes.
        description: What the tool does (for registry
            consumers and documentation).
        handler: The async tool function.
        metadata: Optional annotations (e.g.
            ``requires_auth``, scopes).
    """

    name: str
    description: str
    handler: Callable[..., Any]
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Registry of the agent's tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Register a tool (the last registration wins per name)."""
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        """Return the tool registered under ``name``."""
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"Unknown tool: {name}") from None

    def build_tool_list(self) -> list[Callable[..., Any]]:
        """Return the registered handlers in registration order."""
        return [spec.handler for spec in self._tools.values()]


def build_default_registry() -> ToolRegistry:
    """Register the four OmniCare tools.

    Returns:
        A registry with ``query_policy``, ``get_claim_status``,
        ``search_claims``, and ``prepare_claim_submission``.
    """
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="query_policy",
            description=(
                "Search OmniCare insurance policy documents to answer coverage questions."
            ),
            handler=query_policy,
            metadata={"requires_auth": True},
        )
    )
    registry.register(
        ToolSpec(
            name="get_claim_status",
            description=(
                "Look up the status of a specific claim by its confirmation token or claim ID."
            ),
            handler=get_claim_status,
            metadata={"requires_auth": True},
        )
    )
    registry.register(
        ToolSpec(
            name="search_claims",
            description=("Search the authenticated user's claims history using natural language."),
            handler=search_claims,
            metadata={"requires_auth": True},
        )
    )
    registry.register(
        ToolSpec(
            name="prepare_claim_submission",
            description="Validate and prepare a claim submission for confirmation.",
            handler=prepare_claim_submission,
            metadata={"requires_auth": True},
        )
    )
    return registry
