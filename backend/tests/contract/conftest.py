"""
Shared fixtures and the golden-comparison helper for the Tier A HTTP contract suite.

Tier A freezes the ``/api/v1`` wire format. Every scenario in this package compares a
live response against a JSON file under ``tests/contract/golden/``. A diff in value, key
order, or the presence of an extra key fails the test.

The agent's model is replaced with ``ScriptedLlm`` so the real ADK ``Runner`` still
performs tool dispatch and event construction, while the model's own output is fixed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.contract.llm_double import ScriptedLlm, ScriptedTurn

GOLDEN_DIR = Path(__file__).parent / "golden"

# Non-secret fixture credential used for contract users. It authenticates nothing real.
_DEFAULT_TEST_PASSWORD = "ContractPass123!"

_UPDATE_GOLDENS = False


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the ``--contract-update-goldens`` flag used to re-record goldens."""
    parser.addoption(
        "--contract-update-goldens",
        action="store_true",
        default=False,
        help="Rewrite Tier A golden files from the live responses instead of comparing.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Publish the flag and register the contract marker."""
    global _UPDATE_GOLDENS  # noqa: PLW0603
    _UPDATE_GOLDENS = bool(config.getoption("--contract-update-goldens"))
    config.addinivalue_line("markers", "contract: Tier A golden HTTP contract tests")


def _strip_nested(payload: Any, volatile_keys: frozenset[str]) -> Any:
    """Recursively drop volatile keys from nested dicts and lists."""
    if isinstance(payload, dict):
        return {
            key: _strip_nested(value, volatile_keys)
            for key, value in payload.items()
            if key not in volatile_keys
        }
    if isinstance(payload, list):
        return [_strip_nested(item, volatile_keys) for item in payload]
    return payload


def compare_to_golden(
    name: str,
    actual: Any,
    *,
    volatile_keys: frozenset[str] = frozenset(),
    transform: Callable[[Any], Any] | None = None,
) -> None:
    """Assert that ``actual`` matches the golden JSON file ``name``.

    The comparison is structural and order-sensitive: ``json.dumps`` preserves dict
    insertion order, so a re-ordered payload is reported as a drift just like a changed
    value or an extra key. Goldens are re-recorded deliberately with
    ``pytest --contract-update-goldens``.

    Args:
        name: Golden file stem, e.g. ``"chat_response"``.
        actual: The live payload to compare.
        volatile_keys: Keys stripped from the payload before comparing, for values that
            legitimately vary per run (tokens, UUIDs, timestamps, claim ids).
        transform: Optional final normalisation applied after scrubbing, for values that
            are embedded inside strings rather than exposed as their own key.

    Raises:
        AssertionError: If the golden file is missing or the payload differs.
    """
    golden_path = GOLDEN_DIR / f"{name}.json"
    scrubbed = _strip_nested(actual, volatile_keys)
    if transform is not None:
        scrubbed = transform(scrubbed)

    if _UPDATE_GOLDENS:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(
            json.dumps(scrubbed, indent=2) + "\n",
            encoding="utf-8",
        )
        return

    assert golden_path.exists(), (
        f"Missing golden file {golden_path}. Record it once with: pytest --contract-update-goldens"
    )

    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    actual_text = json.dumps(scrubbed, indent=2)
    if actual_text != json.dumps(expected, indent=2):
        raise AssertionError(
            f"Contract drift for '{name}'.\n"
            f"golden file: {golden_path}\n"
            f"--- expected ---\n{json.dumps(expected, indent=2)}\n"
            f"--- actual ---\n{actual_text}"
        )


@dataclass(frozen=True)
class SeededUser:
    """A signed-up user plus the credentials Tier A needs to act as them.

    Attributes:
        user_id: The user's primary key UUID as a string.
        username: The username used at signup.
        password: The plaintext password used at signup.
        token: A valid access token for the user.
        headers: Bearer authorization headers.
        cookies: The session cookie jar as ``{name: value}``.
    """

    user_id: str
    username: str
    password: str
    token: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


def signup(
    client: TestClient,
    username: str,
    password: str = _DEFAULT_TEST_PASSWORD,  # noqa: S107 - fixture credential, not a secret
) -> SeededUser:
    """Register a user through the public signup endpoint.

    Signup is used rather than a direct database insert so that Tier A exercises the
    real hashing, uniqueness check, and cookie issuance path.

    Args:
        client: The test client.
        username: Unique username for this user.
        password: Password to register with.

    Returns:
        SeededUser: The created user's id, token, headers, and cookies.

    Raises:
        AssertionError: If signup does not return 201.
    """
    response = client.post(
        "/api/v1/auth/signup",
        json={"username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    token = body["access_token"]
    return SeededUser(
        user_id=body["user_id"],
        username=username,
        password=password,
        token=token,
        headers={"Authorization": f"Bearer {token}"},
        cookies={"omnicare_access_token": token},
    )


@pytest.fixture
def contract_client(test_client: TestClient) -> TestClient:
    """The FastAPI test client used by every Tier A scenario."""
    return test_client


def _unique(prefix: str) -> str:
    """Return a per-test unique username so seed fixtures can never collide.

    Collision safety does not rely on the database being clean. Tier A must still
    prove the endpoint contract when a stale row from an interrupted run is present.
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def user_a(contract_client: TestClient) -> SeededUser:
    """A registered user who owns the seeded claims."""
    return signup(contract_client, _unique("contract_user_a"))


@pytest.fixture
def user_b(contract_client: TestClient) -> SeededUser:
    """A second registered user, used for IDOR negatives."""
    return signup(contract_client, _unique("contract_user_b"))


@pytest.fixture
def install_scripted_llm() -> Iterator[Callable[[ScriptedTurn], ScriptedLlm]]:
    """Return a factory that swaps the agent's model for a ``ScriptedLlm``.

    Installation is explicit rather than autouse so a scenario that must observe a real
    failure can leave the real model in place. The real model is restored on teardown.

    The agent is built lazily on first use (plan 07), so ``get_runner()`` is
    called first to force construction before the model is captured.

    Yields:
        Callable[[ScriptedTurn], ScriptedLlm]: Installer returning the installed double.
    """
    from app.agent import agent as agent_module  # noqa: PLC0415

    agent_module.get_runner()  # plan 07: the agent is built lazily on first use
    original = agent_module.omnicare_agent.model
    installed: list[ScriptedLlm] = []

    def _install(turn: ScriptedTurn) -> ScriptedLlm:
        double = ScriptedLlm(turn=turn)
        agent_module.omnicare_agent.model = double
        installed.append(double)
        return double

    yield _install

    agent_module.omnicare_agent.model = original
