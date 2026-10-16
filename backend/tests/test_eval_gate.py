"""CI quality gate for the OmniCare RAG evaluation.

The gate is the only thing standing between a change to the retriever, the
agent, or the evaluation harness and a silent regression in the numbers those
produce. It runs in CI with no API key, on the isolated test database, and it
drives the same ``run_evaluation`` entry point and the same
``app.domain.evaluation_cli`` summary that an operator runs, so the numbers CI
gates on are the numbers the CLI reports.

Three things are checked here:

1. The retrieval machinery still surfaces the sections the dataset expects
   (``recall@5`` and ``mrr`` floors over the answerable subset).
2. The refusal machinery still detects an answer that invents coverage on a
   question the corpus cannot answer, and the metric that reports it is still
   computed -- not vacuously satisfied by an empty subset.
3. The live end-to-end run, which scores the real agent, stays opt-in behind
   ``OMNICARE_EVAL_LIVE`` so CI never spends model calls or flakiness budget on
   it by default.

The answer generator used for the refusal check is a deterministic double, not
the agent: it decides from the retrieved text alone whether the text answers the
question. That makes the check meaningful -- a double that simply returned the
gold answer would pass whatever the metric computed -- while keeping the gate
free of a network call.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from typing import Any

import litellm
import pytest
import pytest_asyncio

from app.domain.evaluation import (
    ANSWER_SOURCE_CONTEXT_PROXY,
    EVAL_DATASET,
    EvalConfig,
    ExpectedBehavior,
    OFFLINE_REFUSAL_FLOORS,
    OFFLINE_FLOORS,
    check_floors,
    expected_behavior_of,
    get_eval_dataset_by_behavior,
    lexically_reachable,
    live_evaluation_enabled,
    looks_like_refusal,
    refusal_verdict,
    run_evaluation,
    states_coverage_claim,
)
from app.domain.evaluation_cli import format_summary
from ragit.evaluation.dataset import EvalSample
from ragit.evaluation.runner import EvalResult

EMBEDDING_DIM = 1536


# ---------------------------------------------------------------------------
# Deterministic doubles
# ---------------------------------------------------------------------------


def _eval_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Return a deterministic hashing embedding for ``text``.

    Words, light stems, and character tri- and quad-grams are all hashed into
    the vector. The character n-grams are what make this double a usable
    stand-in: word tokens alone leave "coverage" and "covered", or "property"
    and "Personal Property Protection", at an L2 distance beyond the product's
    retrieval threshold, and a double that cannot reach half the corpus would
    measure the double rather than the retriever.

    No network call, and identical input always produces the identical vector.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    words: list[str] = []
    for token in tokens:
        words.append(token)
        if len(token) > 4 and token.endswith("s"):
            words.append(token[:-1])
        if len(token) > 5 and token.endswith("ing"):
            words.append(token[:-3])
        if len(token) > 4 and token.endswith("ed"):
            words.append(token[:-2])

    joined = f" {' '.join(tokens)} "
    features = words + re.findall(r"[a-z]{3}", joined) + re.findall(r"[a-z]{4}", joined)

    vector = [0.0] * dim
    for feature in features:
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:2], "big") % dim
        vector[index] += 1.0 if digest[2] % 2 == 0 else -1.0

    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm > 0.0 else vector


class _EmbeddingResponse:
    """Mimic the LiteLLM embedding response shape (``response.data``)."""

    def __init__(self, inputs: list[str]) -> None:
        self.data = [{"embedding": _eval_embedding(item)} for item in inputs]


def _eval_embedding_double(**kwargs: Any) -> _EmbeddingResponse:
    """Replace the LiteLLM embedding call with the deterministic double."""
    inputs = kwargs.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    return _EmbeddingResponse(list(inputs))


_CONTENT_WORDS = re.compile(r"[a-z0-9]+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _substantive_words(text: str) -> set[str]:
    """Return the stems of ``text`` that could, on their own, make it about a subject.

    Function words are dropped, then so is anything under five characters:
    "home", "car", and "hit" are the words that accidentally tie an out-of-corpus
    question to a section heading, and a rule that counts them makes the double
    answer questions it has no business answering. Each word is reduced to one
    canonical stem, so "flooded" and "flood" match but a single matching word
    pair cannot count twice by yielding both "covered" and "cover".
    """
    words = set()
    for token in _CONTENT_WORDS.findall(text.lower()):
        stem = _stem(token)
        if stem in _content_stopwords() or len(stem) < 5:
            continue
        if any(character.isalpha() for character in stem):
            words.add(stem)
    return words


def _stem(token: str) -> str:
    """Return ``token`` with a trailing plural or tense suffix removed."""
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def _content_stopwords() -> set[str]:
    """Return the function words that carry no subject."""
    from app.domain.evaluation import LEXICAL_STOPWORDS  # noqa: PLC0415

    return set(LEXICAL_STOPWORDS)


async def grounded_answer_fn(query: str, context: str) -> str:
    """Answer ``query`` by quoting the retrieved sentences that address it.

    A deterministic model of the assistant's own Uncertainty Ladder: answer from
    the retrieved text, and decline when the retrieved text does not address the
    question. The rule -- a sentence supports the answer when it shares at least
    two substantive words with the question -- is deliberately crude, and it is
    crude in a way a reader can check: the unanswerable samples are exactly the
    ones where no sentence qualifies.

    It over-refuses on questions the corpus can only reach through the
    embedding model's generalisation ("what is the maximum payout for water
    damage?" has two words in common with the section; "I want to claim for a
    $3,000 laptop" has none). That is why the false-refusal direction is gated
    on the live run, where the agent answers them, and only the refusal
    direction is gated here.

    Args:
        query: The evaluation question.
        context: The context the retriever surfaced for it.

    Returns:
        The sentences that share at least two substantive words with the
        question, or the assistant's own no-information sentence when there are
        none.
    """
    wanted = _substantive_words(query)
    supporting = [
        sentence
        for sentence in _SENTENCE_SPLIT.split(context or "")
        if len(_substantive_words(sentence) & wanted) >= 2
    ]
    if not supporting:
        return (
            "I don't have that information in my policy documents. "
            "Please contact our support team."
        )
    return " ".join(" ".join(supporting).split())


async def inventing_answer_fn(query: str, context: str) -> str:
    """Answer every question the same way, with invented coverage terms.

    This double exists to prove the refusal metric is not a tautology: an answer
    generator that states a limit and a deductible for every question must score
    refusal accuracy zero on the unanswerable subset. If it did not, the metric
    would be measuring nothing and the floor would be decoration.
    """
    return (
        f"Regarding {query.strip()} you are covered up to $10,000 with a $250 "
        "deductible, and gradual damage is excluded."
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _deterministic_embeddings(monkeypatch):
    """Install the deterministic embedding double for every test in this module."""
    monkeypatch.setattr(litellm, "embedding", _eval_embedding_double)
    yield


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    """Keep the gate from writing eval_runs rows into the shared test database."""
    monkeypatch.setenv("OMNICARE_EVAL_PERSIST", "0")


@pytest.fixture
def live_evaluation_off(monkeypatch):
    """Guarantee the process is not opted into a live run.

    Requested only by the tests that must resolve the non-live answer generator
    path, so the live test kept live by the environment the operator set up.
    """
    monkeypatch.delenv("OMNICARE_EVAL_LIVE", raising=False)
    return False


@pytest_asyncio.fixture
async def eval_corpus() -> int:
    """Ingest the real sample policy through the real ingestion path.

    The gate measures the production retrieval path against the production
    corpus. Ingesting here rather than reusing a preloaded corpus keeps the gate
    independent of whichever suite ran last against the shared database.
    """
    from pathlib import Path  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    from app.core.database import async_session_factory  # noqa: PLC0415
    from app.domain.policies.ingestion import ingest_policy  # noqa: PLC0415

    # Pin the golden evaluation corpus independently of deployment overrides.
    corpus = Path(__file__).resolve().parents[1] / "app" / "data" / "sample_policy.md"
    assert corpus.is_file(), f"RAG evaluation corpus missing: {corpus}"
    assert corpus.read_text(encoding="utf-8").strip(), (
        f"RAG evaluation corpus is empty: {corpus}"
    )
    ingested = await ingest_policy(str(corpus))
    assert ingested > 0, f"RAG evaluation ingested zero policy chunks from {corpus}"

    async with async_session_factory() as session:
        indexed = (
            await session.execute(text("SELECT COUNT(*) FROM policy_chunks"))
        ).scalar_one()
        documents = (
            await session.execute(
                text("SELECT COUNT(DISTINCT policy_version_id) FROM policy_chunks")
            )
        ).scalar_one()
    assert documents > 0 and indexed > 0, (
        "RAG evaluation preflight failed: no policy documents or chunks indexed "
        f"(documents={documents}, chunks={indexed}, corpus={corpus})"
    )
    return indexed


@pytest_asyncio.fixture
async def section_texts() -> dict[str, str]:
    """Return the text of every indexed policy section."""
    from app.domain.evaluation import load_policy_section_texts  # noqa: PLC0415

    return await load_policy_section_texts()


@pytest_asyncio.fixture
async def retrieval_run(eval_corpus) -> dict[str, Any]:
    """Score the whole curated dataset in retrieval-only mode."""
    result = await run_evaluation(
        EvalConfig(mode="retrieval-only", n_results=5, judge="heuristic"),
        dataset=list(EVAL_DATASET),
    )
    assert result.retrieval_metrics.get("errors", 0) == 0, (
        "the retrieval stage reported errors:\n" + str(result.retrieval_metrics)
    )
    return result


def _render_breaches(breaches: list[Any]) -> str:
    """Render floor breaches as one diff-friendly block."""
    return "\n".join(str(breach) for breach in breaches)


def _reachable(dataset: list[EvalSample], texts: dict[str, str]) -> Callable[[EvalSample], bool]:
    """Return the reachability predicate for ``dataset`` against ``texts``."""
    if not texts:
        return lambda sample: True
    return lambda sample: lexically_reachable(sample, texts)


# ---------------------------------------------------------------------------
# Retrieval floors
# ---------------------------------------------------------------------------


class TestRetrievalFloors:
    """The retrieval stage must keep surfacing the sections the dataset expects."""

    @pytest.mark.asyncio
    async def test_recall_and_mrr_hold_on_reachable_answerable_samples(
        self, retrieval_run, section_texts
    ):
        """recall@5 and MRR stay above their floors on the answerable subset.

        The floors are checked over the samples the retrieval mechanism can
        reach: a question sharing no content word with any section it expects is
        answerable only by the embedding model's generalisation, and in CI the
        embedding model is a deterministic hash. Measuring those here would fail
        the same way whatever broke, which is the opposite of a signal.
        """
        dataset = list(EVAL_DATASET)
        predicate = _reachable(dataset, section_texts)

        reachable_answerable = [
            sample
            for sample in dataset
            if predicate(sample) and expected_behavior_of(sample) == ExpectedBehavior.ANSWER
        ]
        assert len(reachable_answerable) >= 10, (
            "too few answerable samples are lexically reachable for the retrieval "
            f"floor to mean anything; got {len(reachable_answerable)}"
        )

        breaches = check_floors(
            retrieval_run,
            OFFLINE_FLOORS,
            dataset,
            behavior=ExpectedBehavior.ANSWER,
            sample_predicate=predicate,
        )
        assert not breaches, "RAG retrieval regressed:\n" + _render_breaches(breaches)

    @pytest.mark.asyncio
    async def test_every_dataset_sample_was_scored(self, retrieval_run):
        """A floor computed over a subset that silently lost samples is not a floor."""
        scored = {sample.get("query") for sample in retrieval_run.per_sample_results}
        missing = [sample.query for sample in EVAL_DATASET if sample.query not in scored]
        assert not missing, f"the run dropped {len(missing)} dataset samples: {missing}"

    @pytest.mark.asyncio
    async def test_unanswerable_samples_are_present_and_expected_to_refuse(self):
        """The unanswerable subset exists, so the refusal floor is not vacuous."""
        unanswerable = get_eval_dataset_by_behavior(ExpectedBehavior.REFUSE)
        assert len(unanswerable) >= 5, (
            "the unanswerable subset is too small to gate refusal behaviour on; "
            f"got {len(unanswerable)}"
        )
        for sample in unanswerable:
            assert expected_behavior_of(sample) == ExpectedBehavior.REFUSE
            assert sample.gold_answer.strip(), sample.query


# ---------------------------------------------------------------------------
# Refusal behaviour: the invented-coverage axis
# ---------------------------------------------------------------------------


class TestRefusalBehaviour:
    """The harness must be able to see an answer that invents coverage."""

    def test_states_coverage_claim_flags_invented_limits_and_exclusions(self):
        """The detector fires on every shape an invented coverage claim takes."""
        inventions = [
            "Theft from a vehicle is covered up to $2,500 with a $250 deductible.",
            "Mold remediation is covered at 15 percent of the dwelling limit.",
            "Liability claims are strictly excluded.",
            "You are not covered for this kind of damage.",
            "Coverage is capped at $50,000 per occurrence.",
        ]
        for answer in inventions:
            assert states_coverage_claim(answer), answer

    def test_states_coverage_claim_accepts_genuine_refusals(self):
        """Only assertions count, so a refusal is never mistaken for an invention.

        A refusal has to talk about the thing it declines to state -- "no
        deductible can be quoted for it" -- and penalising that would make the
        metric punish the exact behaviour it exists to reward.
        """
        refusals = [
            "I don't have that information in my policy documents. "
            "Please contact our support team.",
            "The policy documents do not address theft from a vehicle, so no "
            "deductible can be quoted for it.",
            "Liability is not addressed in the documents I have, so I'm connecting "
            "you with one of our human specialists who can help you with this.",
            "Weather is unrelated to the policy documents; I can only help with "
            "coverage, existing claims, or new claims.",
        ]
        for answer in refusals:
            assert not states_coverage_claim(answer), answer
            assert looks_like_refusal(answer), answer

    def test_refusal_verdict_separates_the_two_directions(self):
        """Misjudging in either direction is a different bug, and each is tested."""
        # A sample that expects an answer and gets one.
        answered = "Yes, covered up to $25,000 with a $500 deductible."
        assert expected_behavior_of(EVAL_DATASET[0]) == ExpectedBehavior.ANSWER
        assert refusal_verdict(answered, ExpectedBehavior.ANSWER)

        # The same answer on a sample that expects a refusal is a failure.
        assert not refusal_verdict(answered, ExpectedBehavior.REFUSE)

        # A correct refusal on an answerable sample is the opposite failure.
        assert not refusal_verdict(
            "I don't have that information in my policy documents. "
            "Please contact our support team.",
            ExpectedBehavior.ANSWER,
        )

    @pytest.mark.asyncio
    async def test_refusal_accuracy_floor_holds_for_a_grounded_answer_fn(self, eval_corpus):
        """An answer generator that answers from the text scores the floor.

        This is the CI half of the refusal gate: the double decides from the
        retrieved text alone whether the text answers the question, and the
        unanswerable samples are exactly the ones where it does not.
        """
        result = await run_evaluation(
            EvalConfig(mode="end-to-end", n_results=5, judge="heuristic"),
            answer_fn=grounded_answer_fn,
            dataset=list(EVAL_DATASET),
        )

        assert result.answer_metrics["answer_source"] == "injected"
        assert result.answer_metrics["refusal_expected"] >= 5, result.answer_metrics
        assert result.answer_metrics["refusal_accuracy"] == 1.0, result.answer_metrics

        breaches = check_floors(result, OFFLINE_REFUSAL_FLOORS, list(EVAL_DATASET))
        assert not breaches, "refusal behaviour regressed:\n" + _render_breaches(breaches)

    @pytest.mark.asyncio
    async def test_refusal_accuracy_is_not_a_tautology(self, eval_corpus):
        """An answer generator that invents coverage must fail the refusal floor.

        Without this the floor above could pass because the metric never fires.
        """
        result = await run_evaluation(
            EvalConfig(mode="end-to-end", n_results=5, judge="heuristic"),
            answer_fn=inventing_answer_fn,
            dataset=list(EVAL_DATASET),
        )

        assert result.answer_metrics["refusal_accuracy"] == 0.0, result.answer_metrics
        breaches = check_floors(result, OFFLINE_REFUSAL_FLOORS, list(EVAL_DATASET))
        assert any(breach.metric == "refusal_accuracy" for breach in breaches), (
            "an answer generator that states a limit, deductible, and exclusion "
            "for every question was not flagged; the refusal metric is not live"
        )
        # The failure names the samples rather than only the number.
        refusal_breaches = [breach for breach in breaches if breach.metric == "refusal_accuracy"]
        for breach in refusal_breaches:
            assert "samples that stated" in breach.detail
            assert any(sample.query in breach.detail for sample in EVAL_DATASET)

    @pytest.mark.asyncio
    async def test_answer_source_records_which_generator_ran(
        self, eval_corpus, live_evaluation_off
    ):
        """A result must say what produced its answers, so a substitute cannot be
        read as the model."""
        result = await run_evaluation(
            EvalConfig(mode="end-to-end", n_results=5, judge="heuristic"),
            dataset=list(EVAL_DATASET),
        )
        assert result.answer_metrics["answer_source"] == ANSWER_SOURCE_CONTEXT_PROXY
        assert ANSWER_SOURCE_CONTEXT_PROXY in result.summary
        assert "no answer generator was available" in result.summary

    @pytest.mark.asyncio
    async def test_the_cli_summary_is_diff_friendly(self):
        """The CLI summary is a diff target, so it must be stable and ordered.

        Two things make a summary usable in a code review: identical input
        produces identical text, and the fields always appear in the same order
        with the same rounding, so a one-metric change is a one-line diff. A
        wall-clock value would break both, so none is printed.
        """
        config = EvalConfig(mode="retrieval-only", n_results=5, judge="heuristic")
        result = EvalResult(
            timestamp="2026-01-01T00:00:00+00:00",
            config={"mode": "retrieval-only", "judge": "heuristic", "n_results": 5},
            retrieval_metrics={
                "recall_at_k": 0.95,
                "precision_at_k": 0.8,
                "mrr": 0.9,
                "errors": 0,
                "avg_latency_ms": 12.3456,
                "total_queries": 18,
            },
            answer_metrics={
                "answer_source": "agent",
                "avg_faithfulness": 0.87654,
                "avg_relevance": 0.71234,
                "avg_completeness": 0.81234,
                "avg_conciseness": 0.91234,
                "avg_overall": 0.81234,
                "refusal_expected": 5,
                "refusal_accuracy": 1.0,
                "false_refusal_rate": 0.0,
            },
            per_sample_results=[{"query": sample.query} for sample in EVAL_DATASET],
            summary="a summary from the runner",
            duration_seconds=42.0,
        )

        first = format_summary(result, config)
        assert format_summary(result, config) == first

        expected_prefix = [
            "mode: retrieval-only",
            "samples: 18",
            "judge: heuristic",
            "retrieval_n_results: 5",
            "recall@5: 0.9500",
            "precision@5: 0.8000",
            "mrr: 0.9000",
            "retrieval_errors: 0",
            "answer_source: agent",
            "answer metrics:",
            "avg_faithfulness: 0.8765",
            "avg_relevance: 0.7123",
            "avg_completeness: 0.8123",
            "avg_conciseness: 0.9123",
            "avg_overall: 0.8123",
            "refusal metrics:",
            "unanswerable_samples: 5",
            "refusal_accuracy: 1.0000",
            "false_refusal_rate: 0.0000",
        ]
        assert first.splitlines()[: len(expected_prefix)] == expected_prefix
        assert first.splitlines()[len(expected_prefix) :] == [
            "",
            "note: answers were generated by agent.",
        ]

        # Wall-clock values are what make a summary unusable as a diff target.
        assert "42.0" not in first
        assert "12.3456" not in first
        assert "2026-01-01" not in first


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

# A commit sha used only by the persistence test, so its rows can be deleted
# back out without touching anything another run wrote.
_PERSISTENCE_PROBE_SHA = "eval-gate-persistence-probe"


class TestEvalRunsPersistence:
    """A completed run leaves a row a later run can read the trend from."""

    @pytest_asyncio.fixture
    async def persistence_on(self, monkeypatch):
        """Turn persistence on and scope every row this test writes to one sha."""
        from app.domain import evaluation_runs as er  # noqa: PLC0415

        monkeypatch.setenv("OMNICARE_EVAL_PERSIST", "1")
        monkeypatch.setenv("OMNICARE_GIT_SHA", _PERSISTENCE_PROBE_SHA)
        # The sha lookup is cached process-wide, so a test that ran earlier in
        # this process would otherwise pin this one's rows to its own sha.
        er.resolve_git_sha.cache_clear()
        try:
            yield
        finally:
            await er.delete_eval_runs(_PERSISTENCE_PROBE_SHA)
            er.resolve_git_sha.cache_clear()

    @pytest.mark.asyncio
    async def test_a_run_persists_and_reads_back(self, eval_corpus, persistence_on):
        """The metric payload survives the round trip through the table.

        Without this, the write path could drift from the table shape -- a
        renamed key, an unset column -- and the failure would only surface the
        first time somebody queried the trend.
        """
        from app.domain import evaluation_runs as er  # noqa: PLC0415

        result = await run_evaluation(
            EvalConfig(mode="retrieval-only", n_results=5, judge="heuristic"),
            dataset=list(EVAL_DATASET),
        )
        assert result.per_sample_results

        rows = await er.list_eval_runs(10)
        stored = [row for row in rows if row["git_sha"] == _PERSISTENCE_PROBE_SHA]
        assert (
            len(stored) == 1
        ), f"expected exactly one run row for {_PERSISTENCE_PROBE_SHA}, got {len(stored)}"
        row = stored[0]

        assert row["mode"] == "retrieval-only"
        assert row["config"]["n_results"] == 5
        assert row["sample_count"] == len(EVAL_DATASET)
        assert row["metrics"]["samples"] == len(EVAL_DATASET)
        assert row["metrics"]["retrieval"]["total_queries"] == len(EVAL_DATASET)
        assert row["metrics"]["retrieval"]["errors"] == 0
        # The refusal axis is what this track added; it has to be in the payload
        # or the trend cannot show whether it moved.
        assert row["metrics"]["per_sample"][0]["expected_behavior"] in {"answer", "refuse"}
        assert row["created_at"] is not None

    @pytest.mark.asyncio
    async def test_persistence_can_be_switched_off(self, eval_corpus, monkeypatch):
        """A hermetic run writes nothing, so two runs cannot share a trend."""
        from app.domain import evaluation_runs as er  # noqa: PLC0415

        monkeypatch.setenv("OMNICARE_EVAL_PERSIST", "0")
        monkeypatch.setenv("OMNICARE_GIT_SHA", _PERSISTENCE_PROBE_SHA)

        before = await er.list_eval_runs(50)
        await run_evaluation(
            EvalConfig(mode="retrieval-only", n_results=5, judge="heuristic"),
            dataset=list(EVAL_DATASET),
        )
        after = await er.list_eval_runs(50)

        probe_before = [r for r in before if r["git_sha"] == _PERSISTENCE_PROBE_SHA]
        probe_after = [r for r in after if r["git_sha"] == _PERSISTENCE_PROBE_SHA]
        assert probe_after == probe_before == []


# ---------------------------------------------------------------------------
# Live end-to-end (opt-in)
# ---------------------------------------------------------------------------


def test_live_end_to_end_is_opt_in():
    """A live run must not happen unless the environment asks for one.

    If this fails, the process is opted in: the next test runs against the real
    agent, which is the intended way to spend the model calls.
    """
    assert not live_evaluation_enabled(), (
        "OMNICARE_EVAL_LIVE is set in this environment; the offline half of the "
        "gate is now running alongside the live half"
    )


@pytest.mark.skipif(
    not live_evaluation_enabled(),
    reason="Set OMNICARE_EVAL_LIVE=1 to run the end-to-end evaluation against the real agent",
)
@pytest.mark.asyncio
async def test_live_end_to_end_holds_the_live_floors(eval_corpus):
    """Score the real agent over the whole dataset and gate it on the live floors.

    This is the check the whole track is for: ``avg_faithfulness`` here is
    computed against the agent's own answer, so a model that invents coverage on
    an unanswerable question shows up as a refusal floor breach rather than as a
    near-perfect context score.
    """
    from app.domain.evaluation import LIVE_FLOORS  # noqa: PLC0415

    result = await run_evaluation(
        EvalConfig(mode="end-to-end", n_results=5, judge="heuristic"),
        dataset=list(EVAL_DATASET),
    )

    assert result.answer_metrics["answer_source"] == "agent"
    breaches = check_floors(result, LIVE_FLOORS, list(EVAL_DATASET))
    assert not breaches, "live end-to-end evaluation regressed:\n" + _render_breaches(breaches)
