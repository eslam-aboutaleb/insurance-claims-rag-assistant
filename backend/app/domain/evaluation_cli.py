"""Command-line entry point for the OmniCare RAG evaluation.

CI and a developer's terminal run the *same* code path through this module.
When the gate test builds its own runner wiring, the numbers CI checks stop
being the numbers the CLI reports, and a regression can pass one while failing
the other; removing that duplication is what this module is for.

    python -m app.domain.evaluation_cli --mode retrieval --dataset stratified
    python -m app.domain.evaluation_cli --mode end-to-end --dataset unanswerable

``end-to-end`` is opt-in (``--live`` or ``OMNICARE_EVAL_LIVE=1``) because it
makes one real model call per sample. ``retrieval`` is the default and needs no
API key, so it is what CI runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any

from app.domain.evaluation import (
    LIVE_EVAL_ENV,
    LIVE_FLOORS,
    OFFLINE_FLOORS,
    ANSWER_SOURCE_CONTEXT_PROXY,
    EvalConfig,
    EvalResult,
    EVAL_DATASET,
    ExpectedBehavior,
    QuestionCategory,
    expected_behavior_of,
    enable_live_evaluation,
    check_floors,
    lexically_reachable,
    live_evaluation_enabled,
    load_policy_section_texts,
    run_evaluation,
)
from ragit.evaluation.dataset import Difficulty, EvalSample

logger = logging.getLogger(__name__)

MODE_ALIASES = {
    # Both spellings are accepted so a reader who knows ragit's vocabulary and a
    # reader who only knows this module's flags both get the mode they meant.
    "retrieval": "retrieval-only",
    "retrieval-only": "retrieval-only",
    "context": "context",
    "end-to-end": "end-to-end",
}
"""Accepted ``--mode`` spellings mapped onto ragit's mode names."""

DATASET_SELECTORS = ("stratified", "answerable", "unanswerable")
"""Accepted ``--dataset`` values."""

EXIT_OK = 0
EXIT_FLOOR_BREACH = 1
EXIT_USAGE = 2


def select_dataset(
    name: str,
    category: QuestionCategory | None = None,
    difficulty: Difficulty | None = None,
) -> list[EvalSample]:
    """Return the samples a ``--dataset`` selector names.

    Args:
        name: One of :data:`DATASET_SELECTORS`.
        category: Optional category filter, applied before the selector.
        difficulty: Optional difficulty filter, applied before the selector.

    Returns:
        The selected samples, in curated order.

    Raises:
        ValueError: When the selector or a filter value is unknown.
    """
    selected = list(EVAL_DATASET)
    if category is not None:
        selected = [sample for sample in selected if sample.category == category]
    if difficulty is not None:
        selected = [sample for sample in selected if sample.difficulty == difficulty]

    if name == "stratified":
        return selected
    if name == "answerable":
        return [s for s in selected if expected_behavior_of(s) == ExpectedBehavior.ANSWER]
    if name == "unanswerable":
        return [s for s in selected if expected_behavior_of(s) == ExpectedBehavior.REFUSE]
    raise ValueError(f"unknown dataset selector {name!r}; valid: {list(DATASET_SELECTORS)}")


def parse_category(value: str | None) -> QuestionCategory | None:
    """Convert a ``--category`` value into a :class:`QuestionCategory`.

    Args:
        value: The raw flag value, or None when the flag was omitted.

    Returns:
        The category, or None when no filter was requested.

    Raises:
        ValueError: When the value is not a known category.
    """
    if not value:
        return None
    try:
        return QuestionCategory(value.lower())
    except ValueError:
        valid = [category.value for category in QuestionCategory]
        raise ValueError(f"unknown category {value!r}; valid: {valid}") from None


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for this module."""
    parser = argparse.ArgumentParser(
        prog="python -m app.domain.evaluation_cli",
        description="Run the OmniCare RAG evaluation and check it against quality floors.",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(MODE_ALIASES),
        default="retrieval",
        help="Evaluation mode (default: retrieval). end-to-end requires --live.",
    )
    parser.add_argument(
        "--dataset",
        choices=list(DATASET_SELECTORS),
        default="stratified",
        help="Which samples to run (default: stratified).",
    )
    parser.add_argument(
        "--judge",
        choices=("heuristic", "llm"),
        default="heuristic",
        help="Answer evaluation strategy (default: heuristic).",
    )
    parser.add_argument(
        "--n-results",
        type=int,
        default=5,
        help="Retrieval depth (default: 5).",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Filter by question category.",
    )
    parser.add_argument(
        "--difficulty",
        choices=[difficulty.value for difficulty in Difficulty],
        default=None,
        help="Filter by difficulty level.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            f"Permit a live end-to-end run (equivalent to {LIVE_EVAL_ENV}=1). "
            "Spends one real model call per sample."
        ),
    )
    parser.add_argument(
        "--no-floors",
        action="store_true",
        help="Report the metrics without failing on a floor breach.",
    )
    parser.add_argument(
        "--all-samples",
        action="store_true",
        help=(
            "Check the retrieval floors over every sample, including questions the "
            "corpus cannot reach lexically. Off by default: those measure the "
            "embedding model's generalisation, not the retrieval machinery."
        ),
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not write an eval_runs row for this run.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the full result as JSON to this path.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the stable summary, not the per-sample detail.",
    )
    return parser


def format_summary(result: EvalResult, config: EvalConfig) -> str:
    """Render the stable, diff-friendly report for a finished run.

    Stability is the point: the field order is fixed, nothing volatile is
    printed (no wall-clock timestamps, no latency, no duration), and every value
    is rounded, so two runs of the same inputs produce two reports that diff as
    empty.

    Args:
        result: The finished evaluation result.
        config: The configuration the run executed with.

    Returns:
        The report text.
    """
    retrieval = result.retrieval_metrics or {}
    answer = result.answer_metrics or {}

    lines = [
        f"mode: {config.mode}",
        f"samples: {len(result.per_sample_results or [])}",
        f"judge: {config.judge}",
        f"retrieval_n_results: {config.n_results}",
        f"recall@{config.n_results}: {retrieval.get('recall_at_k', 0.0):.4f}",
        f"precision@{config.n_results}: {retrieval.get('precision_at_k', 0.0):.4f}",
        f"mrr: {retrieval.get('mrr', 0.0):.4f}",
        f"retrieval_errors: {retrieval.get('errors', 0)}",
    ]

    if answer:
        answer_source = str(answer.get("answer_source", "unknown"))
        lines.extend(
            [
                f"answer_source: {answer_source}",
                "answer metrics:",
                f"avg_faithfulness: {answer.get('avg_faithfulness', 0.0):.4f}",
                f"avg_relevance: {answer.get('avg_relevance', 0.0):.4f}",
                f"avg_completeness: {answer.get('avg_completeness', 0.0):.4f}",
                f"avg_conciseness: {answer.get('avg_conciseness', 0.0):.4f}",
                f"avg_overall: {answer.get('avg_overall', 0.0):.4f}",
                "refusal metrics:",
                f"unanswerable_samples: {answer.get('refusal_expected', 0)}",
                f"refusal_accuracy: {answer.get('refusal_accuracy', 0.0):.4f}",
                f"false_refusal_rate: {answer.get('false_refusal_rate', 0.0):.4f}",
                *_answer_source_note(answer_source),
            ]
        )

    return "\n".join(lines)


def _answer_source_note(answer_source: str) -> list[str]:
    """Return the provenance note that follows a run's answer metrics.

    An unlabelled number from the retrieved-context substitute is exactly the
    defect this module was written to remove, so the note is not optional.
    """
    if answer_source == ANSWER_SOURCE_CONTEXT_PROXY:
        return [
            "",
            "note: no model was run; the retrieved context was scored in place of the "
            "model's answer. avg_faithfulness here describes the retriever. Re-run with "
            "--live to score the agent.",
        ]
    return ["", f"note: answers were generated by {answer_source}."]


def format_samples(result: EvalResult, dataset: list[EvalSample], limit: int = 8) -> str:
    """Render the per-sample section of the report, weakest sample first.

    Args:
        result: The finished evaluation result.
        dataset: The dataset the run scored, supplying the expected behaviour of
            samples whose result entries are not annotated.
        limit: Maximum number of samples to list.

    Returns:
        The per-sample report text, or an empty string when there is nothing to
        report.
    """
    samples = result.per_sample_results or []
    if not samples:
        return ""

    expected_by_query = {sample.query: expected_behavior_of(sample) for sample in dataset}
    ranked = sorted(samples, key=lambda s: (float(s.get("recall", 0.0)), float(s.get("mrr", 0.0))))
    lines = ["", f"per-sample (weakest {min(limit, len(ranked))} of {len(ranked)}):"]
    for sample in ranked[:limit]:
        query = str(sample.get("query"))
        behavior = expected_by_query.get(query, ExpectedBehavior.ANSWER).value
        lines.append(
            f"  [{behavior}] recall={sample.get('recall')} mrr={sample.get('mrr')} "
            f"query={query!r}"
        )
        answer = sample.get("generated_answer")
        if isinstance(answer, str) and answer.strip():
            lines.append(f"      answer: {' '.join(answer.split())[:200]}")
    return "\n".join(lines)


async def evaluate(args: argparse.Namespace) -> int:
    """Run the evaluation described by ``args`` and report it.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The process exit code: 0 on success, 1 on a floor breach, 2 on a usage
        error or a run that could not execute.
    """
    if args.live:
        enable_live_evaluation()
    if args.no_persist:
        # Read through the environment rather than a module flag so the CLI and
        # every other caller honour the same switch.
        os.environ["OMNICARE_EVAL_PERSIST"] = "0"

    try:
        config = EvalConfig(
            mode=MODE_ALIASES[args.mode],
            judge=args.judge,
            n_results=args.n_results,
            category=parse_category(args.category),
            difficulty=Difficulty(args.difficulty) if args.difficulty else None,
        )
        dataset = select_dataset(
            args.dataset,
            category=config.category,
            difficulty=config.difficulty,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if config.mode == "end-to-end" and not live_evaluation_enabled():
        # A run that silently scores the context while claiming end-to-end is
        # the defect this module exists to remove, so the CLI refuses the
        # ambiguous case instead of producing numbers nobody can trust.
        print(
            "error: --mode end-to-end spends one real model call per sample. "
            f"Pass --live or set {LIVE_EVAL_ENV}=1 to confirm that is intended.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    logger.info(
        "Running OmniCare evaluation: mode=%s dataset=%s samples=%d",
        config.mode,
        args.dataset,
        len(dataset),
    )

    try:
        result = await run_evaluation(config, dataset=dataset)
    except Exception as exc:
        logger.error("Evaluation run failed: %s", exc, exc_info=True)
        print(f"error: evaluation run failed: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print("\n" + format_summary(result, config))
    if not args.quiet:
        print(format_samples(result, dataset))

    if args.output:
        _write_json(result, args.output)

    if args.no_floors:
        return EXIT_OK

    floors = LIVE_FLOORS if live_evaluation_enabled() else OFFLINE_FLOORS
    predicate = None if args.all_samples else await _reachability_predicate(dataset)
    breaches = check_floors(
        result,
        floors,
        dataset,
        behavior=ExpectedBehavior.ANSWER,
        sample_predicate=predicate,
    )
    if breaches:
        print("\nfloors: FAILED")
        for breach in breaches:
            print(f"  {breach}")
        return EXIT_FLOOR_BREACH

    print("\nfloors: OK")
    return EXIT_OK


async def _reachability_predicate(
    dataset: list[EvalSample],
) -> Callable[[EvalSample], bool] | None:
    """Return the filter that drops samples the corpus cannot reach lexically.

    Returns None when the policy is not indexed: with no corpus text to compare
    against, every sample would look unreachable and the floor would be dropped
    silently, which is worse than checking all of them.

    Args:
        dataset: The samples the run scored.

    Returns:
        The predicate, or None when reachability cannot be determined.
    """
    section_texts = await load_policy_section_texts()
    if not section_texts:
        return None
    return lambda sample: lexically_reachable(sample, section_texts)


def _write_json(result: EvalResult, path: str) -> None:
    """Write the full result to ``path`` as JSON.

    The per-sample context is dropped: it is the bulk of the payload, it is
    already in the database as ``policy_chunks``, and keeping it makes the file
    unreadable next to the numbers it documents.
    """
    payload: dict[str, Any] = asdict(result)
    for sample in payload.get("per_sample_results", []):
        sample.pop("context", None)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    print(f"\ndetailed results written to {path}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the evaluation CLI.

    Args:
        argv: Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
