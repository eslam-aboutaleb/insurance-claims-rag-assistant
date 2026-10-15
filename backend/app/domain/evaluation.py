"""OmniCare RAG evaluation wiring over ragit.

The evaluation harness (retrieval metrics, answer judges,
the two-phase runner) is domain-agnostic (ragit); this
module is the OmniCare binding: the curated evaluation
dataset grounded in ``sample_policy.md`` and the
``run_evaluation()`` entry point that wires the OmniCare
hybrid retriever, the curated dataset, and the OmniCare
answer generator into ragit's runner.

What each mode actually measures -- the names are only
honest if this stays true:

* ``retrieval-only`` -- retrieval metrics only.
* ``context`` -- retrieval metrics plus answer metrics
  computed against the *retrieved context*. This is a
  diagnosis of the retriever, not of the model: a high
  ``avg_faithfulness`` here says the answer is in the
  chunks, nothing more.
* ``end-to-end`` -- retrieval metrics plus answer metrics
  computed against the *agent's* answer. ``avg_faithfulness``
  here is the fraction of the model's own words that the
  retrieved context supports, which is the number the name
  promises.

``end-to-end`` therefore always resolves an answer
generator, in this order: the caller's injected
``answer_fn``, a process-wide default installed with
:func:`set_default_answer_fn`, the live agent when live
evaluation is enabled, and finally an explicitly labelled
retrieved-context substitute. Which one ran is reported in
``answer_metrics["answer_source"]`` so a reader can never
mistake a substitute's numbers for the model's.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from app.core.config import get_settings
from app.domain.evaluation_runs import record_eval_run
from app.domain.policies.retriever import retrieve_hybrid
from ragit.evaluation.dataset import Difficulty, EvalSample
from ragit.evaluation.runner import (
    AnswerFunction,
    EvalConfig,
    EvalResult,
    RetrievalFunction,
    run_evaluation as _ragit_run_evaluation,
)

logger = logging.getLogger(__name__)

LIVE_EVAL_ENV = "OMNICARE_EVAL_LIVE"
"""Environment variable that opts a process into spending real LLM calls on evaluation."""

ANSWER_SOURCE_INJECTED = "injected"
ANSWER_SOURCE_DEFAULT = "default"
ANSWER_SOURCE_AGENT = "agent"
ANSWER_SOURCE_CONTEXT_PROXY = "offline:retrieved-context"
"""Label used when no model could be run and the retrieved context was scored instead."""

ANSWER_SOURCE_RETRIEVED_CONTEXT = "retrieved-context"
"""Label used in ``context`` mode, where the retrieved context is deliberately what is scored."""

BEHAVIOR_METADATA_KEY = "expected_behavior"
"""Key under ``EvalSample.metadata`` carrying a sample's expected behaviour."""

#: Synthetic identity the agent runs under during evaluation. It is derived
#: rather than hard-coded so it cannot collide with a real user id, and it is
#: stable so repeated runs hit the same (claim-free) session namespace.
EVAL_USER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "https://omnicare.invalid/eval-harness")


class QuestionCategory(StrEnum):
    """Categories of questions for stratified evaluation."""

    COVERAGE = "coverage"
    LIMITS = "limits"
    EXCLUSIONS = "exclusions"
    DEDUCTIBLES = "deductibles"
    REQUIREMENTS = "requirements"
    GENERAL = "general"


class ExpectedBehavior(StrEnum):
    """What a correct assistant answer looks like for a sample.

    Only ``ANSWER`` was representable before, which made the dataset a set of
    questions the corpus can answer and left the failure mode that actually
    costs money -- answering an unanswerable question with invented coverage --
    completely unmeasured.
    """

    ANSWER = "answer"
    REFUSE = "refuse"


def expected_behavior_of(sample: EvalSample) -> ExpectedBehavior:
    """Return the behaviour a sample expects, defaulting to :attr:`ExpectedBehavior.ANSWER`.

    Args:
        sample: The evaluation sample to inspect.

    Returns:
        The sample's declared behaviour, or ``answer`` when it declares none.
    """
    declared = sample.metadata.get(BEHAVIOR_METADATA_KEY)
    if isinstance(declared, ExpectedBehavior):
        return declared
    if isinstance(declared, str):
        try:
            return ExpectedBehavior(declared.lower())
        except ValueError:
            logger.warning(
                "Sample %r declares unknown expected_behavior %r; treating it as 'answer'.",
                sample.query,
                declared,
            )
    return ExpectedBehavior.ANSWER


def _refusal_sample(**kwargs: Any) -> EvalSample:
    """Build an unanswerable sample, stamping its expected behaviour.

    Args:
        **kwargs: Forwarded to :class:`~ragit.evaluation.dataset.EvalSample`.

    Returns:
        The sample with ``expected_behavior`` recorded in its metadata.
    """
    metadata = dict(kwargs.pop("metadata", None) or {})
    metadata[BEHAVIOR_METADATA_KEY] = ExpectedBehavior.REFUSE
    return EvalSample(metadata=metadata, **kwargs)


# ---------------------------------------------------------------------------
# Curated evaluation dataset grounded in sample_policy.md
# ---------------------------------------------------------------------------

EVAL_DATASET: list[EvalSample] = [
    # --- Water Damage Coverage (Section 1) ---
    EvalSample(
        query="Is water damage from a pipe burst covered?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "Yes, water damage caused by sudden pipe bursts is covered "
            "up to $25,000 with a $500 deductible."
        ),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="What is the maximum payout for water damage?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=("Water damage caused by sudden pipe bursts is covered up to $25,000."),
        category=QuestionCategory.LIMITS,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="How much is the deductible for water damage claims?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=("The deductible for water damage from sudden pipe bursts is $500."),
        category=QuestionCategory.DEDUCTIBLES,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="Does the policy cover gradual water leaks?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "No, gradual leaks are strictly excluded from water damage coverage. "
            "Only sudden pipe bursts are covered."
        ),
        category=QuestionCategory.EXCLUSIONS,
        difficulty=Difficulty.MEDIUM,
    ),
    EvalSample(
        query="Am I covered for flood damage?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "No, flood damage is strictly excluded from the water damage coverage. "
            "Only sudden pipe bursts are covered."
        ),
        category=QuestionCategory.EXCLUSIONS,
        difficulty=Difficulty.MEDIUM,
    ),
    EvalSample(
        query="My bathroom pipe suddenly burst and flooded the floor. Am I covered?",
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "Yes, water damage caused by sudden pipe bursts is covered up to $25,000 "
            "with a $500 deductible."
        ),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.MEDIUM,
        metadata={"scenario": "real-world"},
    ),
    # --- Personal Property Coverage (Section 2) ---
    EvalSample(
        query="What is covered under personal property protection?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=("Electronics, furniture, and jewelry are covered up to $10,000 total."),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="What is the coverage limit for personal property?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=(
            "Personal property including electronics, furniture, and jewelry is "
            "covered up to $10,000 total."
        ),
        category=QuestionCategory.LIMITS,
        difficulty=Difficulty.EASY,
    ),
    EvalSample(
        query="Do I need an appraisal for expensive items?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=("Yes, single items exceeding $2,500 require individual appraisal receipts."),
        category=QuestionCategory.REQUIREMENTS,
        difficulty=Difficulty.MEDIUM,
    ),
    EvalSample(
        query="I want to claim for a $3,000 laptop. What do I need?",
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=(
            "Since the laptop exceeds $2,500, you will need an individual appraisal "
            "receipt. Electronics are covered under personal property protection "
            "up to $10,000 total."
        ),
        category=QuestionCategory.REQUIREMENTS,
        difficulty=Difficulty.MEDIUM,
        metadata={"scenario": "real-world"},
    ),
    # --- Cross-section / Edge cases ---
    EvalSample(
        query="What types of claims does OmniCare handle?",
        expected_sections=[
            "Section 1: Home Water Damage Coverage",
            "Section 2: Personal Property Protection",
        ],
        gold_answer=(
            "OmniCare handles water damage claims (sudden pipe bursts, up to $25,000) "
            "and personal property claims (electronics, furniture, jewelry, up to $10,000)."
        ),
        category=QuestionCategory.GENERAL,
        difficulty=Difficulty.HARD,
    ),
    EvalSample(
        query="What are all the coverage limits in the policy?",
        expected_sections=[
            "Section 1: Home Water Damage Coverage",
            "Section 2: Personal Property Protection",
        ],
        gold_answer=(
            "The policy has two coverage limits: water damage from sudden pipe bursts "
            "is covered up to $25,000, and personal property (electronics, furniture, "
            "jewelry) is covered up to $10,000 total."
        ),
        category=QuestionCategory.LIMITS,
        difficulty=Difficulty.HARD,
    ),
    EvalSample(
        query="What things are NOT covered by OmniCare?",
        # Section 1 is the section that states the exclusions, so it is the one
        # retrieval must guarantee. The gold answer also carries Section 2's
        # appraisal requirement -- the omission of which is exactly why the old
        # gold answer was incomplete -- but ``expected_sections`` records what
        # retrieval has to surface, not the full breadth of the answer.
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "Gradual leaks and flood damage are explicitly excluded from the water damage "
            "coverage, and personal property protection requires an individual appraisal "
            "receipt for any single item exceeding $2,500."
        ),
        category=QuestionCategory.EXCLUSIONS,
        difficulty=Difficulty.HARD,
    ),
    # --- Unanswerable / refusal cases ---
    # sample_policy.md has two sections and says nothing about any of the subjects
    # below, so every one of these is a near-miss trap: retrieval surfaces
    # something, and the only correct answer is to decline to state coverage.
    _refusal_sample(
        query="My car was hit by another driver. Does my policy cover the repairs?",
        # Retrieval will surface both sections; neither mentions vehicles.
        expected_sections=[
            "Section 1: Home Water Damage Coverage",
            "Section 2: Personal Property Protection",
        ],
        gold_answer=(
            "The policy documents cover water damage from sudden pipe bursts and "
            "personal property such as electronics, furniture, and jewelry. They say "
            "nothing about vehicles or road accidents, so this cannot be answered "
            "from them."
        ),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.MEDIUM,
        metadata={"scenario": "out-of-corpus"},
    ),
    _refusal_sample(
        query="There is mold spreading across my basement walls. Is mold remediation covered?",
        # The nearest miss in the corpus: "water damage" reads as adjacent to mold
        # to a retriever, and the section says nothing about remediation.
        expected_sections=["Section 1: Home Water Damage Coverage"],
        gold_answer=(
            "The policy documents cover water damage from sudden pipe bursts and "
            "personal property, and neither mentions mold or remediation, so no "
            "coverage statement can be made from them."
        ),
        category=QuestionCategory.COVERAGE,
        difficulty=Difficulty.HARD,
        metadata={"scenario": "near-miss"},
    ),
    _refusal_sample(
        query="Someone smashed my car window and stole my laptop. What deductible applies?",
        # A trap shaped like a deductible question: the only deductible in the
        # corpus belongs to water damage, and quoting it here would be invented.
        expected_sections=["Section 2: Personal Property Protection"],
        gold_answer=(
            "The policy documents do not address theft from a vehicle, so no "
            "deductible can be quoted for it."
        ),
        category=QuestionCategory.DEDUCTIBLES,
        difficulty=Difficulty.HARD,
        metadata={"scenario": "near-miss"},
    ),
    _refusal_sample(
        query="A guest slipped at my home and is now threatening to sue me. Am I covered?",
        # Liability, injury, and legal action are absent from the corpus and the
        # assistant's own guardrails escalate them to a human instead.
        expected_sections=[],
        gold_answer=(
            "Liability, injury, and legal action are not addressed in the policy "
            "documents, so this has to go to a human specialist rather than be "
            "answered from them."
        ),
        category=QuestionCategory.GENERAL,
        difficulty=Difficulty.MEDIUM,
        metadata={"scenario": "out-of-corpus"},
    ),
    _refusal_sample(
        query="What is the weather forecast for Cairo tomorrow?",
        # Out of scope entirely. Nothing in the corpus is close enough to be a
        # near-miss, so no section is expected to be retrieved.
        expected_sections=[],
        gold_answer=(
            "Weather is unrelated to the policy documents, so the assistant should "
            "decline rather than answer from insurance material."
        ),
        category=QuestionCategory.GENERAL,
        difficulty=Difficulty.EASY,
        metadata={"scenario": "out-of-scope"},
    ),
]


def get_eval_dataset(
    category: QuestionCategory | None = None,
    difficulty: Difficulty | None = None,
) -> list[EvalSample]:
    """Return the evaluation dataset, optionally filtered.

    Args:
        category: If provided, only return samples of this category.
        difficulty: If provided, only return samples of this difficulty.

    Returns:
        List of EvalSample matching the filters.
    """
    dataset = EVAL_DATASET
    if category is not None:
        dataset = [s for s in dataset if s.category == category]
    if difficulty is not None:
        dataset = [s for s in dataset if s.difficulty == difficulty]
    return dataset


def get_eval_dataset_by_behavior(behavior: ExpectedBehavior) -> list[EvalSample]:
    """Return only the samples that expect ``behavior``.

    The refusal floor is a ratio over the unanswerable subset, so the gate needs
    that subset as a first-class selection rather than as an ad-hoc filter.

    Args:
        behavior: The expected behaviour to keep.

    Returns:
        Samples whose expected behaviour is ``behavior``.
    """
    return [s for s in EVAL_DATASET if expected_behavior_of(s) == behavior]


# ---------------------------------------------------------------------------
# Refusal behaviour: the invented-coverage detector
# ---------------------------------------------------------------------------

# A claim about coverage terms. Only assertions count: a genuine refusal often
# has to *name* a deductible ("no deductible can be quoted for it") to explain
# why it is declining, and penalising that would make the metric punish the
# behaviour it exists to reward.
_COVERAGE_CLAIM = re.compile(
    # a dollar figure, the single most common way an invented limit appears
    r"\$\s?\d[\d,]*(?:\.\d+)?"
    # a percentage, written either as a sign or spelled out
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:%|percent(?:age)?)"
    # a deductible tied to an amount, e.g. "deductible of $500"
    r"|\bdeductible\b[^.;]{0,24}?\$?\s?\d"
    # an asserted exclusion, e.g. "is strictly excluded" / "are not covered"
    r"|\b(?:is|are|was|were)\s+(?:strictly\s+|explicitly\s+|generally\s+)?"
    r"(?:excluded|not covered|never covered)\b"
    # an asserted cap
    r"|\b(?:up to|capped at|limited to|maximum of)\s+\$?\s?\d",
    re.IGNORECASE,
)

# A cue that the answer is declining rather than answering. The list is
# deliberately broad: a false "answer" verdict on a refusal is the expensive
# error, because it hides the failure this metric was added to expose.
_REFUSAL_CUE = re.compile(
    r"\bi\s+(?:do\s+not|don't)\s+have\s+(?:that|this|the)\b"
    r"|\bi\s+(?:can\s?not|can't)\s+(?:answer|help|provide|determine|confirm|say|tell)\b"
    r"|\bi\s+(?:am|'m)\s+unable\b"
    r"|\bnot\s+(?:addressed|mentioned|covered|discussed|included)\b"
    r"|\b(?:do|does|did)\s+not\s+(?:address|mention|cover|state|say|include|apply)\b"
    r"|\bno\s+(?:information|mention)\s+(?:about|of|on)\b"
    r"|\boutside\s+(?:the\s+scope\s+of\s+)?my\b"
    r"|\bunrelated\s+to\b"
    r"|\bplease\s+contact\s+(?:our\s+)?support\b"
    r"|\bconnecting\s+you\s+with\b"
    r"|\bi'?m\s+sorry\b[^.]{0,60}\b(?:cannot|can't|unable|do\s+not|don't)\b"
    r"|\bwould\s+be\s+(?:inventing|making\s+(?:it\s+)?up|guessing)\b"
    r"|\bcan\s+only\s+help\s+with\b"
    r"|\bso\s+i\s+cannot\b"
    r"|\bnot\s+something\s+i\s+can\b"
    r"|\bno\s+such\s+(?:coverage|thing)\b",
    re.IGNORECASE,
)


def states_coverage_claim(answer: str) -> bool:
    """Return True when ``answer`` asserts a coverage limit, deductible, or exclusion.

    This is the invented-coverage detector. It is intentionally narrow -- only
    assertions match -- because a correct refusal on an unanswerable question
    still has to talk about the thing it is declining to state.

    Args:
        answer: The generated answer text.

    Returns:
        True when the answer makes a claim the corpus has to support.
    """
    return bool(_COVERAGE_CLAIM.search(answer or ""))


def looks_like_refusal(answer: str) -> bool:
    """Return True when ``answer`` declines to answer rather than answering.

    Args:
        answer: The generated answer text.

    Returns:
        True when the answer carries a refusal cue.
    """
    return bool(_REFUSAL_CUE.search(answer or ""))


def refusal_verdict(answer: str, behavior: ExpectedBehavior) -> bool:
    """Return True when ``answer`` behaves the way ``behavior`` requires.

    Args:
        answer: The generated answer text.
        behavior: The behaviour the sample expected.

    Returns:
        True when the answer met the expectation. A sample expecting a refusal
        passes when it states no coverage claim. A sample expecting an answer
        passes when it either states coverage or says nothing about coverage at
        all, and fails only when it carries a refusal cue -- a decline with no
        claim in it is the over-refusal direction, and a decline that still
        quotes the policy is a contradictory answer worth counting as answered.
    """
    text = (answer or "").strip()
    if not text:
        return False
    if behavior == ExpectedBehavior.REFUSE:
        return not states_coverage_claim(text)
    is_refusal = looks_like_refusal(text) and not states_coverage_claim(text)
    return not is_refusal


# ---------------------------------------------------------------------------
# Quality floors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalFloors:
    """Minimum acceptable metrics for an evaluation run.

    Floors exist so a regression is a build failure rather than a number
    somebody notices a week later. ``None`` disables a floor, which is how a
    caller declares "this subset is not scored here" instead of asserting a
    floor of zero that no run could ever break.
    """

    recall_at_k: float = 0.9
    mrr: float = 0.8
    refusal_accuracy: float | None = None
    false_refusal_rate: float | None = None


OFFLINE_FLOORS = EvalFloors(recall_at_k=0.9, mrr=0.8, refusal_accuracy=1.0, false_refusal_rate=0.35)
"""Floors for a deterministic run, where the answer double is ours and exact.

The refusal direction is exact here: the double is a known quantity, so it has
to be perfect. The false-refusal tripwire is loose, because a deterministic
double can only reach the questions the corpus answers lexically and declining
the ones that need the embedding model's generalisation is correct behaviour
rather than a regression. It is not zero, because an answer generator that
refused *everything* would otherwise sail through the refusal floor above.
"""

LIVE_FLOORS = EvalFloors(recall_at_k=0.9, mrr=0.8, refusal_accuracy=0.8, false_refusal_rate=0.2)
"""Floors for a live run, where a real model is allowed some imperfection."""

#: The same floors with the retrieval checks switched off. A caller that scored
#: one thing -- an injected answer generator over a fixed corpus, say -- must not
#: inherit a retrieval verdict about a retrieval it never ran.
OFFLINE_REFUSAL_FLOORS = replace(OFFLINE_FLOORS, recall_at_k=0.0, mrr=0.0)
LIVE_REFUSAL_FLOORS = replace(LIVE_FLOORS, recall_at_k=0.0, mrr=0.0)


# ---
# Lexical reachability
# ---

LEXICAL_STOPWORDS = frozenset(
    """a an the and or of to in on at for with from by is are was were be been being do does did
    have has had i me my we our you your he she it its they them this that these those what which
    who whom when where why how not no yes if then than so as up out off over under again very
    am can could should would will shall may might must want need get got go going make made
    there here about into onto per via""".split()
)
"""Function words a query and a section can share without the query being about that section.

The list exists so "reachability" cannot be satisfied by the pronoun or the
preposition every English sentence carries. A sample counts as lexically
reachable only when some word that carries its meaning also appears in the text
of a section it expects.
"""


def lexically_reachable(sample: EvalSample, section_texts: Mapping[str, str]) -> bool:
    """Return True when some content word of ``sample`` appears in an expected section.

    A retrieval floor can only be asserted over questions the retriever has a
    chance of answering *by the mechanism under test*. A query that shares no
    content word with any expected section -- "what types of claims does OmniCare
    handle?" against a policy that never lists claim types -- is answerable only
    by the embedding model's generalisation, so measuring it here would score the
    double rather than the retrieval machinery and would fail the same way no
    matter what broke.

    The test is mechanical on purpose: it is computed from the corpus, not chosen
    by hand, so a sample cannot be quietly dropped from the floor it fails.

    Args:
        sample: The evaluation sample to test.
        section_texts: The text of every section in the corpus, keyed by section.

    Returns:
        True when at least one content word of the query appears in the text of
        at least one expected section.
    """
    content_words = {
        token
        for token in re.findall(r"[a-z0-9]+", sample.query.lower())
        if token not in LEXICAL_STOPWORDS and len(token) > 2 and any(ch.isalpha() for ch in token)
    }
    if not content_words:
        return False

    for section in sample.expected_sections:
        text = section_texts.get(section, "")
        if not text:
            continue
        if content_words & set(re.findall(r"[a-z0-9]+", text.lower())):
            return True
    return False


async def load_policy_section_texts() -> dict[str, str]:
    """Return the text of every indexed policy section, keyed by section.

    Args:
        None.

    Returns:
        A mapping of section name to chunk text. Empty when the policy has not
        been ingested, in which case the caller should not filter on
        reachability rather than treat every sample as unreachable.
    """
    from sqlalchemy import text as sql_text  # noqa: PLC0415

    from app.core.database import async_session_factory  # noqa: PLC0415

    async with async_session_factory() as session:
        rows = (await session.execute(sql_text("SELECT section, text FROM policy_chunks"))).all()

    sections: dict[str, str] = {}
    for section, body in rows:
        sections[section] = f"{sections.get(section, '')}\n{body or ''}"
    return sections


@dataclass(frozen=True)
class FloorBreach:
    """One metric that fell below its floor.

    Attributes:
        metric: Name of the metric, as it appears in the result.
        floor: The value the metric had to reach.
        actual: The value it reached.
        detail: Which samples contributed the shortfall, when they can be named.
    """

    metric: str
    floor: float
    actual: float
    detail: str = ""

    def __str__(self) -> str:
        """Render the breach as a single diff-friendly line."""
        base = f"{self.metric}: {self.actual:.4f} < floor {self.floor:.4f}"
        return f"{base}\n{self.detail}" if self.detail else base


# ---------------------------------------------------------------------------
# Answer generators
# ---------------------------------------------------------------------------

_default_answer_fn: AnswerFunction | None = None


def set_default_answer_fn(answer_fn: AnswerFunction | None) -> None:
    """Install the process-wide answer generator used when a caller injects none.

    This is the seam a deployment uses to swap in its own model without editing
    the evaluation entry point.

    Args:
        answer_fn: The generator to use, or None to clear the installed one.
    """
    global _default_answer_fn  # noqa: PLW0603
    _default_answer_fn = answer_fn


def live_evaluation_enabled() -> bool:
    """Return True when this process may spend real LLM calls on evaluation.

    A full end-to-end run makes one model call per sample, so it is opt-in:
    without the flag an unattended process cannot silently run up a bill.
    """
    return os.environ.get(LIVE_EVAL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def enable_live_evaluation() -> None:
    """Opt this process into live end-to-end evaluation runs."""
    os.environ[LIVE_EVAL_ENV] = "1"


async def agent_answer_fn(query: str, context: str) -> str:
    """Answer one evaluation query with the real OmniCare agent.

    The agent is the product, so scoring anything else would score a stand-in.

    Args:
        query: The evaluation question, passed to the agent as a user message.
        context: The context ragit retrieved. Ignored: the agent runs the real
            ``query_policy`` tool and therefore retrieves its own. Conditioning
            it on a context it never saw would make the faithfulness score
            measure an answer that does not exist.

    Returns:
        The agent's final text response.
    """
    # Imported here rather than at module scope so that importing the evaluation
    # module does not pull the ADK runtime into every process that reads it.
    from app.agent.agent import reset_user_session, run_agent  # noqa: PLC0415

    # A cold session per sample: a session reused across samples would carry the
    # previous question's answer into this one, and the second sample would be
    # scored on a conversation nobody designed.
    await reset_user_session(str(EVAL_USER_ID))
    result = await run_agent(str(EVAL_USER_ID), query)
    return str(result.get("response") or "")


async def context_proxy_answer_fn(query: str, context: str) -> str:
    """Stand in for a model by returning the retrieved context.

    Only reached when no model can be run. The result it produces is
    indistinguishable from ``context`` mode, which is exactly why the caller is
    required to label the run with :data:`ANSWER_SOURCE_CONTEXT_PROXY`: an
    unlabelled number from here is the defect this module exists to remove.

    Args:
        query: Ignored; nothing is generated, only relayed.
        context: The context ragit retrieved for this sample.

    Returns:
        The retrieved context, or the assistant's own no-information sentence
        when nothing was retrieved.
    """
    if context and context.strip():
        return context
    return "I don't have that information in my policy documents. Please contact our support team."


def resolve_answer_function(
    injected: AnswerFunction | None = None,
) -> tuple[AnswerFunction | None, str]:
    """Decide which answer generator an ``end-to-end`` run will use.

    Args:
        injected: A caller-supplied generator, typically a deterministic double.

    Returns:
        A ``(answer_fn, source_label)`` pair. ``answer_fn`` is None only when no
        generator can be resolved at all, which cannot happen through the
        documented path.
    """
    if injected is not None:
        return injected, ANSWER_SOURCE_INJECTED
    if _default_answer_fn is not None:
        return _default_answer_fn, ANSWER_SOURCE_DEFAULT
    if live_evaluation_enabled():
        return agent_answer_fn, ANSWER_SOURCE_AGENT
    return context_proxy_answer_fn, ANSWER_SOURCE_CONTEXT_PROXY


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_evaluation(
    config: EvalConfig | None = None,
    answer_fn: AnswerFunction | None = None,
    dataset: list[EvalSample] | None = None,
) -> EvalResult:
    """Run a complete RAG evaluation.

    Injects the OmniCare hybrid retriever, the curated evaluation dataset, and
    the resolved answer generator into ragit's runner. ``context`` mode scores
    the retrieved context (a retrieval diagnosis); ``end-to-end`` mode scores
    what the answer generator produced (the model, when one is available).

    Args:
        config: Evaluation configuration. If None, uses ragit's defaults.
        answer_fn: Answer generator ``async (query, context) -> answer`` used
            for ``end-to-end`` mode. When omitted, the generator is resolved by
            :func:`resolve_answer_function` and recorded in the result.
        dataset: Samples to score. Defaults to the curated dataset filtered by
            the config's category and difficulty. Passing it explicitly lets a
            caller score a subset -- the unanswerable samples alone, say --
            without the run's config and its dataset disagreeing.

    Returns:
        EvalResult with all metrics and per-sample details.
    """
    if config is None:
        config = EvalConfig()

    if dataset is None:
        dataset = get_eval_dataset(category=config.category, difficulty=config.difficulty)

    async def _retrieval_fn(query: str) -> list[dict[str, Any]]:
        return await retrieve_hybrid(query=query, n_results=config.n_results)

    answer_source = ANSWER_SOURCE_RETRIEVED_CONTEXT if config.mode == "context" else ""
    resolved_fn: AnswerFunction | None = None
    if config.mode == "end-to-end":
        resolved_fn, answer_source = resolve_answer_function(answer_fn)

    result = await _ragit_run_evaluation(
        config,
        _retrieval_fn,
        dataset,
        settings=get_settings(),
        answer_fn=resolved_fn,
    )

    _annotate_result(result, dataset, answer_source)
    await _persist_result(result, config)
    return result


def _annotate_result(
    result: EvalResult,
    dataset: list[EvalSample],
    answer_source: str,
) -> None:
    """Add the refusal-behaviour metrics and provenance to a finished result.

    ragit reports faithfulness, relevance, completeness, and conciseness. None of
    those notice an answer that invents coverage for a question the corpus
    cannot answer, so a run can look excellent while the model does the one
    thing that costs the company money. These metrics close that hole.

    Args:
        result: The ragit result to annotate, modified in place.
        dataset: The dataset the run scored, in the runner's sample order.
        answer_source: Which answer generator produced the answers.
    """
    if not result.answer_metrics:
        return

    refusal_verdicts: list[bool] = []
    answer_verdicts: list[bool] = []
    regressions: list[str] = []

    for index, sample_result in enumerate(result.per_sample_results):
        behavior = (
            expected_behavior_of(dataset[index])
            if index < len(dataset)
            else _behavior_from_sample(sample_result)
        )
        sample_result[BEHAVIOR_METADATA_KEY] = behavior.value
        verdict = refusal_verdict(sample_result.get("generated_answer", ""), behavior)
        if behavior == ExpectedBehavior.REFUSE:
            refusal_verdicts.append(verdict)
        else:
            answer_verdicts.append(verdict)
        if not verdict:
            regressions.append(f"{behavior.value}: {sample_result.get('query', '')}")

    result.answer_metrics["answer_source"] = answer_source
    result.answer_metrics["refusal_expected"] = len(refusal_verdicts)
    result.answer_metrics["answer_expected"] = len(answer_verdicts)
    result.answer_metrics["refusal_accuracy"] = _ratio(refusal_verdicts)
    result.answer_metrics["false_refusal_rate"] = _ratio([not v for v in answer_verdicts])

    result.summary = "\n".join(
        [
            _answer_source_note(answer_source),
            result.summary,
            "",
            "Refusal Behaviour:",
            f"  Unanswerable samples: {len(refusal_verdicts)}",
            f"  Refusal accuracy: {result.answer_metrics['refusal_accuracy']:.4f}",
            f"  False refusal rate: {result.answer_metrics['false_refusal_rate']:.4f}",
        ]
    )

    if regressions:
        result.summary += "\n  Samples failing their expected behaviour:\n" + "\n".join(
            f"    - {entry}" for entry in regressions
        )


def _behavior_from_sample(sample_result: dict[str, Any]) -> ExpectedBehavior:
    """Recover a sample's expectation from its annotated result entry.

    Only reached when the result carries more entries than the dataset, which
    the runner does not currently do; the fallback keeps the annotation total
    rather than raising on a future runner change.

    Args:
        sample_result: One per-sample entry from an annotated result.

    Returns:
        The recorded behaviour, or ``answer`` when the entry carries none.
    """
    declared = sample_result.get(BEHAVIOR_METADATA_KEY)
    if isinstance(declared, ExpectedBehavior):
        return declared
    if isinstance(declared, str):
        try:
            return ExpectedBehavior(declared.lower())
        except ValueError:
            pass
    return ExpectedBehavior.ANSWER


def _ratio(flags: list[bool]) -> float:
    """Return the fraction of ``flags`` that are True, or 0.0 for an empty list.

    An empty denominator returns zero rather than one on purpose: "no unanswerable
    samples were scored" must not read as "every unanswerable sample behaved
    correctly", because that is the direction that hides a missing subset.
    """
    return round(sum(1 for flag in flags if flag) / len(flags), 4) if flags else 0.0


def _answer_source_note(answer_source: str) -> str:
    """Return the provenance line that leads an annotated summary."""
    if answer_source == ANSWER_SOURCE_CONTEXT_PROXY:
        return (
            "NOTE: no answer generator was available, so the retrieved context was "
            f"scored in place of the model's answer (answer_source={answer_source}). "
            "These answer metrics describe the retriever, not the model. Set "
            f"{LIVE_EVAL_ENV}=1 or inject an answer_fn to score the model."
        )
    return f"Answer source: {answer_source}"


def check_floors(
    result: EvalResult,
    floors: EvalFloors,
    dataset: list[EvalSample] | None = None,
    behavior: ExpectedBehavior | None = None,
    sample_predicate: Callable[[EvalSample], bool] | None = None,
) -> list[FloorBreach]:
    """Compare a finished result against its floors.

    Args:
        result: The evaluation result to check.
        floors: The minimum acceptable metrics.
        dataset: The dataset the run scored. Supplies the expected behaviour per
            query, which a retrieval-only run has not annotated into its result,
            and the sample objects a ``sample_predicate`` is applied to.
        behavior: Restrict the retrieval floors to samples expecting this
            behaviour. Retrieval quality is only meaningful on questions the
            corpus can answer, so the gate checks the answerable subset and lets
            the unanswerable subset be scored by the refusal floors.
        sample_predicate: Optional filter on the dataset, applied before the
            behaviour filter. Used to narrow the retrieval floors to the samples
            the retrieval mechanism can reach.

    Returns:
        One :class:`FloorBreach` per metric that fell short, empty when the run
        passes.
    """
    breaches: list[FloorBreach] = []

    dataset = list(dataset) if dataset else []
    if sample_predicate is not None:
        dataset = [sample for sample in dataset if sample_predicate(sample)]

    subset = _behaviour_subset(result, dataset, behavior)
    if not subset:
        wanted = behavior.value if behavior is not None else "any"
        return [FloorBreach("dataset", 1.0, 0.0, f"no samples expect {wanted!r}")]

    recalls = [float(sample.get("recall", 0.0)) for sample in subset]
    mrrs = [float(sample.get("mrr", 0.0)) for sample in subset]
    recall = sum(recalls) / len(recalls)
    mrr = sum(mrrs) / len(mrrs)

    if recall < floors.recall_at_k:
        breaches.append(
            FloorBreach(
                "recall_at_k",
                floors.recall_at_k,
                round(recall, 4),
                _below_floor(subset, floors.recall_at_k, "recall"),
            )
        )
    if mrr < floors.mrr:
        breaches.append(
            FloorBreach(
                "mrr",
                floors.mrr,
                round(mrr, 4),
                _below_floor(subset, floors.mrr, "mrr"),
            )
        )

    # Refusal behaviour can only be judged on a run that generated answers. A
    # retrieval-only run has none, so its refusal accuracy is not zero -- it is
    # absent, and reporting it as a failure would send a reader hunting for a
    # regression that does not exist.
    has_answers = any("generated_answer" in sample for sample in result.per_sample_results)
    answer_metrics = result.answer_metrics if has_answers else {}
    if floors.refusal_accuracy is not None and has_answers:
        actual = float(answer_metrics.get("refusal_accuracy", 0.0))
        if actual < floors.refusal_accuracy:
            breaches.append(
                FloorBreach(
                    "refusal_accuracy",
                    floors.refusal_accuracy,
                    actual,
                    "samples that stated a limit, deductible, or exclusion on an "
                    "unanswerable question:\n  "
                    + "\n  ".join(_misbehaving(result, dataset, ExpectedBehavior.REFUSE)),
                )
            )
    if floors.false_refusal_rate is not None and has_answers:
        actual = float(answer_metrics.get("false_refusal_rate", 0.0))
        if actual > floors.false_refusal_rate:
            breaches.append(
                FloorBreach(
                    "false_refusal_rate",
                    floors.false_refusal_rate,
                    actual,
                    "answerable samples the answer declined to answer:\n  "
                    + "\n  ".join(_misbehaving(result, dataset, ExpectedBehavior.ANSWER)),
                )
            )

    return breaches


def _behaviour_subset(
    result: EvalResult,
    dataset: list[EvalSample],
    behavior: ExpectedBehavior | None,
) -> list[dict[str, Any]]:
    """Return the per-sample entries the caller selected and wants judged.

    The dataset is the selection. A per-sample entry whose query is not in it
    was deliberately left out -- by a behaviour filter, or by a reachability
    predicate -- so it must not fall back to a default expectation and slip into
    the numbers. Only when no dataset is supplied at all does the annotated
    result become the source, which is what a caller checking a result it did
    not build needs.

    Args:
        result: The evaluation result to filter.
        dataset: The samples the run scored, or an empty list when the caller
            supplied none.
        behavior: The behaviour to keep, or None to keep every selected sample.

    Returns:
        The per-sample entries that were selected and match ``behavior``.
    """
    if not dataset:
        if behavior is None:
            return list(result.per_sample_results)
        selected = result.per_sample_results
        return [sample for sample in selected if _behavior_from_sample(sample) == behavior]

    expected_by_query = {sample.query: expected_behavior_of(sample) for sample in dataset}
    subset = []
    for sample in result.per_sample_results:
        query = str(sample.get("query"))
        if query not in expected_by_query:
            continue
        if behavior is None or expected_by_query[query] == behavior:
            subset.append(sample)
    return subset


def _below_floor(subset: list[dict[str, Any]], floor: float, key: str) -> str:
    """Render the samples that pulled a metric below its floor."""
    regressing = sorted(
        f"{sample.get('query')!r} {key}={sample.get(key)}"
        for sample in subset
        if float(sample.get(key, 0.0)) < floor
    )
    if not regressing:
        return ""
    return "regressions:\n  " + "\n  ".join(regressing)


def _misbehaving(
    result: EvalResult,
    dataset: list[EvalSample],
    behavior: ExpectedBehavior,
) -> list[str]:
    """Name the samples that did not meet ``behavior``, for a failure message.

    A floor breach that does not name the regressing samples is a number nobody
    can act on; each entry carries the query, what went wrong, and an excerpt of
    the answer so the distance between the failure message and its cause is zero.
    """
    expected_by_query = {sample.query: expected_behavior_of(sample) for sample in dataset}
    named = []
    for sample in result.per_sample_results:
        query = str(sample.get("query"))
        if expected_by_query.get(query, _behavior_from_sample(sample)) != behavior:
            continue
        answer = str(sample.get("generated_answer", ""))
        # A retrieval-only run has no answer to judge; it is not a refusal.
        if "generated_answer" not in sample:
            continue
        if refusal_verdict(answer, behavior):
            continue
        reason = (
            "stated a coverage claim"
            if states_coverage_claim(answer)
            else ("declined to answer" if looks_like_refusal(answer) else "no usable answer")
        )
        excerpt = " ".join(answer.split())[:160]
        named.append(f"{query!r}: {reason}; answer={excerpt!r}")
    return sorted(named)


async def _persist_result(result: EvalResult, config: EvalConfig) -> None:
    """Write one row per completed run, never failing the run over it.

    A run whose metrics cannot be stored still produced the metrics; aborting
    here would trade the measurement for its archive.

    Args:
        result: The finished result to store.
        config: The configuration the run used, stored alongside it.
    """
    from app.domain.evaluation_runs import persistence_enabled  # noqa: PLC0415

    if not persistence_enabled():
        return

    metrics = {
        "samples": len(result.per_sample_results),
        "retrieval": result.retrieval_metrics,
        "answer": result.answer_metrics,
        "per_sample": [
            {
                "query": sample.get("query"),
                "expected_behavior": str(sample.get(BEHAVIOR_METADATA_KEY, "answer")),
                "recall": sample.get("recall"),
                "mrr": sample.get("mrr"),
                "answer_faithfulness": sample.get("answer_faithfulness"),
                "answer_overall": sample.get("answer_overall"),
            }
            for sample in result.per_sample_results
        ],
    }

    try:
        await record_eval_run(
            mode=str(config.mode),
            config=dict(result.config or {}),
            metrics=metrics,
        )
    except Exception:
        logger.warning("Could not persist the evaluation run.", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point, delegating to :mod:`app.domain.evaluation_cli`.

    Kept as this module's entry point so the historical
    ``python -m app.domain.evaluation`` invocation keeps working; the CLI
    itself lives in its own module so the runner wiring and the argument
    parsing do not grow into one file.

    Args:
        argv: Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """
    from app.domain.evaluation_cli import main as cli_main  # noqa: PLC0415

    return cli_main(argv)


__all__ = [
    "ANSWER_SOURCE_AGENT",
    "ANSWER_SOURCE_CONTEXT_PROXY",
    "ANSWER_SOURCE_DEFAULT",
    "ANSWER_SOURCE_INJECTED",
    "ANSWER_SOURCE_RETRIEVED_CONTEXT",
    "BEHAVIOR_METADATA_KEY",
    "EVAL_USER_ID",
    "ExpectedBehavior",
    "Difficulty",
    "EVAL_DATASET",
    "EvalConfig",
    "EvalFloors",
    "EvalResult",
    "EvalSample",
    "FloorBreach",
    "LIVE_FLOORS",
    "LIVE_REFUSAL_FLOORS",
    "LIVE_EVAL_ENV",
    "LEXICAL_STOPWORDS",
    "OFFLINE_FLOORS",
    "OFFLINE_REFUSAL_FLOORS",
    "QuestionCategory",
    "RetrievalFunction",
    "agent_answer_fn",
    "check_floors",
    "context_proxy_answer_fn",
    "enable_live_evaluation",
    "expected_behavior_of",
    "get_eval_dataset",
    "get_eval_dataset_by_behavior",
    "lexically_reachable",
    "live_evaluation_enabled",
    "load_policy_section_texts",
    "looks_like_refusal",
    "main",
    "refusal_verdict",
    "resolve_answer_function",
    "run_evaluation",
    "set_default_answer_fn",
    "states_coverage_claim",
]
