"""Coverage tests for the uncovered branches of ``app.domain.evaluation_cli``.

The CLI is the surface operators and CI actually run, so each behaviour pinned
here is one the runner wiring depends on: the dataset selectors, the category
and difficulty filters, the ``--live`` gate that keeps a live run opt-in, the
usage errors, the stability of the printed summary, and the floor verdicts.

Every test drives ``main()`` or ``evaluate()`` in-process with the runner
stubbed, so no retrieval, database, or model call happens -- including the live
path, which is exercised only through its refusal.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest

from app.domain import evaluation_cli as cli
from app.domain.evaluation import (
    ANSWER_SOURCE_CONTEXT_PROXY,
    EVAL_DATASET,
    LIVE_EVAL_ENV,
    FloorBreach,
    QuestionCategory,
    expected_behavior_of,
)
from app.domain.evaluation_cli import (
    DATASET_SELECTORS,
    EXIT_FLOOR_BREACH,
    EXIT_OK,
    EXIT_USAGE,
    MODE_ALIASES,
    build_parser,
    evaluate,
    format_samples,
    format_summary,
    main,
    parse_category,
    select_dataset,
)
from ragit.evaluation.dataset import Difficulty, EvalSample
from ragit.evaluation.runner import EvalConfig, EvalResult

_ANSWERABLE_QUERY = "Is water damage from a pipe burst covered?"
_REFUSAL_QUERY = "What is the weather forecast for Cairo tomorrow?"


def _sample(query: str, *, behavior: str | None = None) -> EvalSample:
    """Build a dataset sample with an optional expected behaviour.

    Args:
        query: The question the sample asks.
        behavior: ``"answer"`` or ``"refuse"`` when the expectation is declared,
            otherwise no expectation is recorded.

    Returns:
        EvalSample: The sample, in the shape ``EVAL_DATASET`` produces.
    """
    metadata = {} if behavior is None else {"expected_behavior": behavior}
    return EvalSample(
        query=query,
        expected_sections=[],
        gold_answer="",
        category="general",
        difficulty=Difficulty.EASY,
        metadata=metadata,
    )


def _catalogue() -> list[EvalSample]:
    """Return a two-sample dataset, one answerable and one refusal sample.

    Returns:
        list[EvalSample]: The dataset the per-sample formatting tests use.
    """
    return [
        _sample(_ANSWERABLE_QUERY, behavior="answer"),
        _sample(_REFUSAL_QUERY, behavior="refuse"),
    ]


def _result(**overrides: Any) -> EvalResult:
    """Build an :class:`EvalResult` the CLI can render and write out.

    Args:
        **overrides: Fields to replace on the result.

    Returns:
        EvalResult: A finished result carrying sane retrieval numbers, one strong
        sample and one weak one.
    """
    payload: dict[str, Any] = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "config": {"mode": "retrieval-only", "judge": "heuristic", "n_results": 5},
        "retrieval_metrics": {
            "recall_at_k": 0.95,
            "precision_at_k": 0.8,
            "mrr": 0.9,
            "errors": 0,
            "avg_latency_ms": 12.3456,
            "total_queries": 2,
        },
        "answer_metrics": {},
        "per_sample_results": [
            {
                "query": _ANSWERABLE_QUERY,
                "recall": 1.0,
                "mrr": 1.0,
                "generated_answer": "Yes, water damage is covered up to $25,000.",
            },
            {
                "query": _REFUSAL_QUERY,
                "recall": 0.5,
                "mrr": 0.5,
                "context": "retrieved context that must not reach the JSON output",
            },
        ],
        "summary": "a summary from the runner",
        "duration_seconds": 42.0,
    }
    payload.update(overrides)
    return EvalResult(**payload)


class CliRig:
    """Driver that runs the CLI with its collaborators stubbed out.

    The runner, the reachability lookup, and the floor check are replaced so a
    test can drive the CLI end to end without touching the database, the corpus,
    or a model. Everything the CLI passed to those collaborators is recorded so
    the test can assert on the wiring instead of on the collaborator.
    """

    def __init__(self) -> None:
        """Initialise the rig with an empty recording."""
        self.recorded: dict[str, Any] = {"run_calls": 0}
        self.section_texts: dict[str, str] = {}

    def __call__(self, argv: list[str]) -> int:
        """Run ``main()`` with the stubs installed."""
        return main(argv)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the CLI's collaborators for the duration of one test.

        Args:
            monkeypatch: The monkeypatch fixture, which undoes every patch.
        """
        recorded = self.recorded
        section_texts = self.section_texts

        async def _fake_run_evaluation(
            config: EvalConfig, dataset: list[EvalSample] | None = None
        ) -> EvalResult:
            recorded["run_calls"] += 1
            recorded["config"] = config
            recorded["dataset"] = dataset
            if "error" in recorded:
                raise recorded["error"]
            return recorded.get("result", _result())

        async def _fake_section_texts() -> dict[str, str]:
            recorded["section_text_calls"] = recorded.get("section_text_calls", 0) + 1
            return section_texts

        def _fake_check_floors(
            result: EvalResult,
            floors: Any,
            dataset: list[EvalSample],
            behavior: Any = None,
            sample_predicate: Any = None,
        ) -> list[Any]:
            recorded["floors"] = floors
            recorded["behavior"] = behavior
            recorded["predicate"] = sample_predicate
            recorded["floor_dataset"] = dataset
            return list(recorded.get("breaches", []))

        def _fake_enable_live() -> None:
            recorded["live_enabled"] = recorded.get("live_enabled", 0) + 1

        monkeypatch.setattr(cli, "run_evaluation", _fake_run_evaluation)
        monkeypatch.setattr(cli, "load_policy_section_texts", _fake_section_texts)
        monkeypatch.setattr(cli, "check_floors", _fake_check_floors)
        monkeypatch.setattr(cli, "enable_live_evaluation", _fake_enable_live)
        monkeypatch.delenv(LIVE_EVAL_ENV, raising=False)
        monkeypatch.delenv("OMNICARE_EVAL_PERSIST", raising=False)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> CliRig:
    """Install the stubbed collaborators and hand the rig to the test."""
    rig = CliRig()
    rig.install(monkeypatch)
    return rig


class TestDatasetSelection:
    """The ``--dataset`` selectors decide what a run scores."""

    def test_stratified_returns_every_curated_sample(self):
        """The default selector is the whole curated set, in curated order."""
        selected = select_dataset("stratified")

        assert selected == list(EVAL_DATASET)

    def test_answerable_keeps_only_samples_that_expect_an_answer(self):
        """The answerable subset is exactly what the behaviour metadata says it is."""
        selected = select_dataset("answerable")

        assert selected
        assert all(expected_behavior_of(sample) == "answer" for sample in selected)

    def test_unanswerable_keeps_only_samples_that_expect_a_refusal(self):
        """The refusal subset is the one the refusal floors are computed over."""
        selected = select_dataset("unanswerable")

        assert selected
        assert all(expected_behavior_of(sample) == "refuse" for sample in selected)
        # The two selectors partition the dataset, so neither can lose samples.
        assert len(select_dataset("answerable")) + len(selected) == len(EVAL_DATASET)

    def test_an_unknown_selector_names_the_valid_ones(self):
        """A typo'd selector is an error, and it says what the valid values are."""
        with pytest.raises(ValueError, match="unknown dataset selector"):
            select_dataset("answerable-ish")

    def test_a_category_filter_is_applied_before_the_selector(self):
        """A ``--category`` narrows what each selector then keeps."""
        selected = select_dataset("stratified", category=QuestionCategory.LIMITS)

        assert selected
        assert all(sample.category == QuestionCategory.LIMITS for sample in selected)

    def test_a_difficulty_filter_is_applied_before_the_selector(self):
        """A ``--difficulty`` narrows the same way, independently of a category."""
        selected = select_dataset("answerable", difficulty=Difficulty.HARD)

        assert selected
        assert all(sample.difficulty == Difficulty.HARD for sample in selected)

    def test_every_documented_selector_is_accepted(self):
        """Each documented selector returns a non-empty selection of the dataset."""
        for name in DATASET_SELECTORS:
            assert select_dataset(name), name


class TestParseCategory:
    """The ``--category`` flag is converted into an enum or a usage error."""

    def test_a_missing_value_requests_no_filter(self):
        """An omitted flag must not silently filter on a default category."""
        assert parse_category(None) is None
        assert parse_category("") is None

    def test_a_known_category_is_returned(self):
        """A valid category name maps onto its enum member."""
        assert parse_category("limits") is QuestionCategory.LIMITS

    def test_the_match_is_case_insensitive(self):
        """A capitalised value is accepted so the flag is not finicky."""
        assert parse_category("Deductibles") is QuestionCategory.DEDUCTIBLES

    def test_an_unknown_category_lists_the_valid_ones(self):
        """The error names the valid values so a typo is self-correcting."""
        with pytest.raises(ValueError, match="unknown category 'mould'"):
            parse_category("mould")


class TestArgumentParser:
    """The parser is the CLI's contract with its callers."""

    def test_the_defaults_match_what_ci_runs(self):
        """Omitting every flag yields the deterministic, offline run CI uses."""
        args = build_parser().parse_args([])

        assert args.mode == "retrieval"
        assert args.dataset == "stratified"
        assert args.judge == "heuristic"
        assert args.n_results == 5
        assert args.category is None
        assert args.difficulty is None
        assert args.live is False
        assert args.no_floors is False
        assert args.all_samples is False
        assert args.no_persist is False
        assert args.output is None
        assert args.quiet is False

    def test_every_mode_alias_parses(self):
        """Both spellings of a mode are accepted, so the alias table is reachable."""
        for alias in MODE_ALIASES:
            assert build_parser().parse_args(["--mode", alias]).mode == alias

    def test_an_unknown_mode_is_rejected_before_anything_runs(self):
        """A misspelled mode exits with a usage status rather than reaching the run."""
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--mode", "offline"])

        assert excinfo.value.code == EXIT_USAGE

    def test_an_unknown_dataset_is_rejected_before_anything_runs(self):
        """A misspelled dataset selector is refused by the parser too."""
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--dataset", "everything"])

        assert excinfo.value.code == EXIT_USAGE


class TestRunSelection:
    """The parsed flags decide what the runner is asked to score."""

    def test_a_retrieval_run_maps_the_alias_onto_the_harness_mode(self, wired: CliRig):
        """``--mode retrieval`` is the harness's ``retrieval-only``; the alias holds."""
        exit_code = wired(["--mode", "retrieval", "--dataset", "stratified"])
        recorded = wired.recorded

        assert exit_code == EXIT_OK
        assert recorded["config"].mode == "retrieval-only"
        assert recorded["config"].judge == "heuristic"
        assert recorded["config"].n_results == 5
        assert recorded["dataset"] == list(EVAL_DATASET)

    def test_context_mode_runs_without_a_live_flag(self, wired: CliRig):
        """``context`` scores the retriever, so it needs no model call."""
        exit_code = wired(["--mode", "context", "--dataset", "answerable"])
        recorded = wired.recorded

        assert exit_code == EXIT_OK
        assert recorded["config"].mode == "context"
        assert all(expected_behavior_of(sample) == "answer" for sample in recorded["dataset"])

    def test_a_difficulty_flag_reaches_the_config(self, wired: CliRig):
        """A parsed difficulty is handed to the runner as a ``Difficulty`` member."""
        exit_code = wired(["--difficulty", "hard", "--dataset", "unanswerable"])
        recorded = wired.recorded

        assert exit_code == EXIT_OK
        assert recorded["config"].difficulty is Difficulty.HARD
        assert all(sample.difficulty == Difficulty.HARD for sample in recorded["dataset"])


class TestLiveGate:
    """A live end-to-end run happens only when the environment asks for one."""

    def test_end_to_end_without_the_live_flag_is_refused(self, wired: CliRig, capsys):
        """The ambiguous case -- no flag, no model -- is refused, not scored.

        The CLI is the one place that can tell a context score apart from a model
        score, so a run that would report one as the other must not start at all.
        """
        exit_code = wired(["--mode", "end-to-end"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_USAGE
        assert wired.recorded["run_calls"] == 0
        assert "error: --mode end-to-end spends one real model call per sample" in captured.err
        assert "--live" in captured.err
        assert f"{LIVE_EVAL_ENV}=1" in captured.err

    def test_the_live_flag_permits_end_to_end_and_selects_the_live_floors(
        self, wired: CliRig, monkeypatch
    ):
        """``--live`` is the explicit confirmation, and it switches the floors.

        A live run is allowed model imperfection, so it is held to the live
        floors rather than the exact ones an offline run is held to.
        """
        monkeypatch.setattr(cli, "live_evaluation_enabled", lambda: True)

        exit_code = wired(["--mode", "end-to-end", "--live"])
        recorded = wired.recorded

        assert exit_code == EXIT_OK
        assert recorded["live_enabled"] == 1
        assert recorded["run_calls"] == 1
        assert recorded["floors"] is cli.LIVE_FLOORS

    def test_the_environment_variable_alone_also_permits_it(self, wired: CliRig, monkeypatch):
        """``OMNICARE_EVAL_LIVE=1`` is equivalent to ``--live`` for the gate."""
        monkeypatch.setenv(LIVE_EVAL_ENV, "1")

        exit_code = wired(["--mode", "end-to-end"])

        assert exit_code == EXIT_OK
        assert wired.recorded["run_calls"] == 1
        assert wired.recorded["floors"] is cli.LIVE_FLOORS


class TestUsageErrors:
    """A request the CLI cannot run is a usage error, not a crash."""

    def test_an_unknown_category_is_a_usage_error(self, wired: CliRig, capsys):
        """The category filter is validated before the run starts."""
        exit_code = wired(["--category", "mould"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_USAGE
        assert wired.recorded["run_calls"] == 0
        assert "error: unknown category 'mould'" in captured.err

    def test_an_unknown_dataset_selector_is_a_usage_error(self, capsys):
        """A selector the dataset cannot honour exits 2 rather than running."""
        args = build_parser().parse_args(["--dataset", "stratified"])
        args.dataset = "answerable-ish"

        exit_code = asyncio.run(evaluate(args))

        captured = capsys.readouterr()
        assert exit_code == EXIT_USAGE
        assert "error: unknown dataset selector" in captured.err

    def test_an_unknown_difficulty_is_a_usage_error(self, capsys):
        """A difficulty outside the enum never reaches the runner either."""
        args = build_parser().parse_args([])
        args.difficulty = "impossible"

        exit_code = asyncio.run(evaluate(args))

        captured = capsys.readouterr()
        assert exit_code == EXIT_USAGE
        assert "error:" in captured.err

    def test_a_run_that_could_not_execute_is_reported_once(self, wired: CliRig, capsys):
        """A failed run is reported as an error, not as a floor breach."""
        wired.recorded["error"] = RuntimeError("policy index not available")

        exit_code = wired(["--mode", "retrieval"])

        captured = capsys.readouterr()
        assert exit_code == EXIT_USAGE
        assert "error: evaluation run failed: policy index not available" in captured.err
        assert "floors:" not in captured.out


class TestReportedOutput:
    """What the CLI prints is the operator's view of the run."""

    def test_the_summary_and_the_weakest_samples_are_printed(self, wired: CliRig, capsys):
        """The stable summary leads, and the per-sample detail follows by default."""
        exit_code = wired(["--mode", "retrieval"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_OK
        assert "mode: retrieval-only" in captured.out
        assert "recall@5: 0.9500" in captured.out
        assert "per-sample" in captured.out
        # The weakest sample is listed first, so a reader sees the problem first.
        listed = [line for line in captured.out.splitlines() if line.startswith("  [")]
        assert listed[0].startswith("  [refuse]")
        assert _ANSWERABLE_QUERY in captured.out

    def test_quiet_prints_only_the_summary(self, wired: CliRig, capsys):
        """``--quiet`` drops the per-sample section, keeping the diff target."""
        exit_code = wired(["--mode", "retrieval", "--quiet"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_OK
        assert "mode: retrieval-only" in captured.out
        assert "per-sample" not in captured.out

    def test_the_output_file_carries_the_result_without_the_context(
        self, wired: CliRig, capsys, tmp_path
    ):
        """The JSON file keeps the numbers and drops the retrieved context.

        The context is the bulk of the payload and is already in the database as
        ``policy_chunks``, so writing it out would only make the file unreadable.
        """
        destination = tmp_path / "eval-run.json"

        exit_code = wired(["--mode", "retrieval", "--output", str(destination)])
        captured = capsys.readouterr()

        assert exit_code == EXIT_OK
        payload = json.loads(destination.read_text(encoding="utf-8"))
        assert payload["retrieval_metrics"]["recall_at_k"] == 0.95
        assert all("context" not in sample for sample in payload["per_sample_results"])
        assert f"detailed results written to {destination}" in captured.out

    def test_no_persist_flips_the_environment_switch(self, wired: CliRig, monkeypatch):
        """``--no-persist`` is read through the environment, not a module flag.

        The switch is pre-seeded so ``monkeypatch`` owns its lifetime: the CLI
        writes ``os.environ`` directly as a side effect of the flag, and leaving
        that write in place would leak ``OMNICARE_EVAL_PERSIST=0`` into every
        test that runs afterwards.
        """
        monkeypatch.setenv("OMNICARE_EVAL_PERSIST", "1")

        exit_code = wired(["--mode", "retrieval", "--no-persist"])

        assert exit_code == EXIT_OK
        assert os.environ["OMNICARE_EVAL_PERSIST"] == "0"

    def test_the_refusal_metrics_are_reported(self, wired: CliRig, capsys, monkeypatch):
        """Refusal accuracy and the false-refusal tripwire are in the summary.

        These are the only numbers that can see an answer inventing coverage on
        an unanswerable question, so their absence would be a silent hole.
        """
        monkeypatch.setattr(cli, "live_evaluation_enabled", lambda: True)
        answer_metrics = {
            "answer_source": "agent",
            "avg_faithfulness": 0.9,
            "avg_relevance": 0.8,
            "avg_completeness": 0.7,
            "avg_conciseness": 0.6,
            "avg_overall": 0.75,
            "refusal_expected": 2,
            "refusal_accuracy": 1.0,
            "false_refusal_rate": 0.25,
        }
        wired.recorded["result"] = _result(answer_metrics=answer_metrics)

        exit_code = wired(["--mode", "end-to-end"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_OK
        assert "answer_source: agent" in captured.out
        assert "refusal metrics:" in captured.out
        assert "unanswerable_samples: 2" in captured.out
        assert "refusal_accuracy: 1.0000" in captured.out
        assert "false_refusal_rate: 0.2500" in captured.out
        assert "note: answers were generated by agent." in captured.out


class TestFloorVerdicts:
    """The exit code is the contract CI reads, so each verdict is pinned."""

    def test_a_passing_run_prints_floors_ok(self, wired: CliRig, capsys):
        """A run inside its floors says so, and exits clean."""
        exit_code = wired(["--mode", "retrieval"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_OK
        assert "floors: OK" in captured.out

    def test_a_breach_is_listed_and_fails_the_run(self, wired: CliRig, capsys):
        """A floor breach names the metric and the samples that pulled it down."""
        wired.recorded["breaches"] = [
            FloorBreach("recall_at_k", 0.9, 0.5, "regressions:\n  'the query'")
        ]

        exit_code = wired(["--mode", "retrieval"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_FLOOR_BREACH
        assert "floors: FAILED" in captured.out
        assert "recall_at_k: 0.5000 < floor 0.9000" in captured.out
        assert "'the query'" in captured.out

    def test_no_floors_reports_metrics_without_a_verdict(self, wired: CliRig, capsys):
        """``--no-floors`` measures without failing a build on the numbers."""
        wired.recorded["breaches"] = [FloorBreach("mrr", 0.8, 0.1)]

        exit_code = wired(["--mode", "retrieval", "--no-floors"])
        captured = capsys.readouterr()

        assert exit_code == EXIT_OK
        assert "floors:" not in captured.out
        assert "mode: retrieval-only" in captured.out

    def test_the_retrieval_floors_are_checked_on_the_answerable_subset(self, wired: CliRig):
        """Retrieval quality is only meaningful where the corpus can answer."""
        wired(["--mode", "retrieval", "--dataset", "stratified"])
        recorded = wired.recorded

        assert recorded["behavior"] == cli.ExpectedBehavior.ANSWER
        assert recorded["floor_dataset"] == list(EVAL_DATASET)

    def test_all_samples_checks_every_sample_including_unreachable_ones(self, wired: CliRig):
        """``--all-samples`` drops the reachability filter rather than computing it."""
        wired(["--mode", "retrieval", "--all-samples"])

        assert wired.recorded["predicate"] is None
        assert wired.recorded.get("section_text_calls", 0) == 0

    def test_the_default_run_filters_out_samples_the_corpus_cannot_reach(self, wired: CliRig):
        """By default a sample the corpus cannot reach lexically is not floored.

        Otherwise the floor would measure the embedding model's generalisation
        and fail the same way whatever broke.
        """
        wired.section_texts["Section 1: Home Water Damage Coverage"] = (
            "Water damage from a sudden pipe burst is covered up to $25,000 with a "
            "$500 deductible."
        )

        wired(["--mode", "retrieval", "--dataset", "stratified"])
        predicate = wired.recorded["predicate"]

        assert callable(predicate)
        reachable = [sample for sample in EVAL_DATASET if predicate(sample)]
        assert reachable, "the predicate dropped every sample"
        assert any(not predicate(sample) for sample in EVAL_DATASET)

    def test_an_unindexed_corpus_checks_every_sample(self, wired: CliRig):
        """With no corpus text the filter is skipped rather than applied vacuously."""
        wired(["--mode", "retrieval", "--dataset", "stratified"])

        # An empty section-text mapping means reachability cannot be determined.
        assert wired.recorded["predicate"] is None


class TestFormatSummary:
    """The summary is a diff target, so it must be complete and stable."""

    def test_a_retrieval_only_run_reports_no_answer_metrics(self):
        """A run that never generated answers must not report answer numbers."""
        config = EvalConfig(mode="retrieval-only", n_results=5, judge="heuristic")

        rendered = format_summary(_result(), config)

        assert "answer metrics:" not in rendered
        assert "refusal metrics:" not in rendered
        assert rendered.splitlines()[0] == "mode: retrieval-only"
        assert rendered.splitlines()[1] == "samples: 2"

    def test_an_empty_metrics_dict_rounds_to_zero(self):
        """Nothing retrieved renders as zeros, not as a missing line."""
        config = EvalConfig(mode="retrieval-only", n_results=5, judge="heuristic")

        rendered = format_summary(_result(retrieval_metrics={}, per_sample_results=[]), config)

        assert "recall@5: 0.0000" in rendered
        assert "retrieval_errors: 0" in rendered

    def test_a_context_proxy_answer_source_is_labelled_as_such(self):
        """The substitute's numbers are reported with a note saying what they are.

        An unlabelled number from the retrieved-context substitute is exactly the
        defect this module exists to remove, so the note is not optional.
        """
        config = EvalConfig(mode="end-to-end", n_results=5, judge="heuristic")
        result = _result(
            answer_metrics={"answer_source": ANSWER_SOURCE_CONTEXT_PROXY, "avg_overall": 0.5}
        )

        rendered = format_summary(result, config)

        assert "answer_source: offline:retrieved-context" in rendered
        assert "note: no model was run; the retrieved context was scored" in rendered
        assert "--live to score the agent" in rendered


class TestFormatSamples:
    """The per-sample section is what a reader acts on after a breach."""

    def test_no_samples_produce_no_section(self):
        """An empty result yields an empty string, not an empty header."""
        assert format_samples(_result(per_sample_results=[]), _catalogue()) == ""

    def test_the_weakest_sample_comes_first(self):
        """Ordering is by the recovery metrics, so the problem is at the top."""
        result = _result(
            per_sample_results=[
                {"query": _ANSWERABLE_QUERY, "recall": 1.0, "mrr": 1.0},
                {"query": _REFUSAL_QUERY, "recall": 0.1, "mrr": 0.2},
            ]
        )

        rendered = format_samples(result, _catalogue())

        lines = rendered.splitlines()
        assert lines[1] == "per-sample (weakest 2 of 2):"
        assert lines[2].startswith("  [refuse]")
        assert "recall=0.1" in lines[2]
        assert lines[3].startswith("  [answer]")
        assert "recall=1.0" in lines[3]

    def test_at_most_the_limit_samples_are_listed(self):
        """A long result is truncated to the limit, and the header says so."""
        samples = [
            {"query": f"question {index}", "recall": index / 100, "mrr": 0.0} for index in range(10)
        ]

        rendered = format_samples(_result(per_sample_results=samples), _catalogue(), limit=3)

        body = [line for line in rendered.splitlines() if line.startswith("  [")]
        assert "per-sample (weakest 3 of 10):" in rendered
        assert len(body) == 3

    def test_a_sample_absent_from_the_dataset_is_assumed_answerable(self):
        """A stray entry is not silently dropped, and it defaults to ``answer``."""
        result = _result(per_sample_results=[{"query": "not in the dataset", "recall": 0.5}])

        rendered = format_samples(result, _catalogue())

        assert "[answer]" in rendered
        assert "'not in the dataset'" in rendered

    def test_the_answer_excerpt_is_flattened_and_truncated(self):
        """A multi-line answer becomes one line, capped so the report stays readable."""
        result = _result(
            per_sample_results=[
                {
                    "query": _ANSWERABLE_QUERY,
                    "recall": 1.0,
                    "mrr": 1.0,
                    "generated_answer": "first line\nsecond\n" + ("word " * 80),
                }
            ]
        )

        rendered = format_samples(result, _catalogue())

        line = [line for line in rendered.splitlines() if "answer:" in line][0]
        excerpt = line.split("answer:", 1)[1].strip()
        assert len(excerpt) <= 200
        assert "first line second" in excerpt

    def test_a_blank_or_non_string_answer_is_not_reported(self):
        """An empty answer, or one that is not text, adds no line to the report."""
        result = _result(
            per_sample_results=[
                {
                    "query": _ANSWERABLE_QUERY,
                    "recall": 1.0,
                    "mrr": 1.0,
                    "generated_answer": "   ",
                },
                {
                    "query": _REFUSAL_QUERY,
                    "recall": 0.5,
                    "mrr": 0.5,
                    "generated_answer": 42,
                },
            ]
        )

        rendered = format_samples(result, _catalogue())

        assert "answer:" not in rendered
