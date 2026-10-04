"""Tier B assertion 1: the query-class matrix against real PostgreSQL.

Every query class from the plan (exact-keyword, semantic, synonym,
ambiguous, follow-up, out-of-scope, no-result, conflicting-documents)
runs through the production ``retrieve_hybrid`` path and must surface
the sections recorded in ``query_matrix.json``.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import pytest

from app.domain.policies.retriever import retrieve_hybrid
from app.domain.embeddings import get_vector_store
from tests.integration.conftest import (
    BACKEND_DIR,
    hash_embedding,
    section_chunk_ids,
)

MATRIX_PATH = Path(__file__).parent / "query_matrix.json"
BASELINE_PATH = BACKEND_DIR / "tests" / "baselines" / "rag_baseline.json"
DISTANCE_THRESHOLD = 1.3


def _load_matrix() -> dict[str, Any]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    return ordered[lower] * (upper - rank) + ordered[upper] * (rank - lower)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    _load_matrix()["queries"],
    ids=lambda entry: entry["id"],
)
async def test_query_class_surfaces_expected_sections(
    entry: dict[str, Any],
    tierb_seed: dict[str, Any],
):
    """Each query class retrieves exactly the sections it must."""
    results = await retrieve_hybrid(entry["query"], n_results=5)

    if not entry["expected_sections"]:
        assert results == [], f"expected no results for {entry['query']!r}, got {results}"
        return

    assert results, f"no results for query {entry['query']!r}"
    top_section = results[0]["metadata"]["section"]
    assert top_section in entry["expected_sections"], (
        f"top result for {entry['query']!r} was {top_section!r}; "
        f"expected one of {entry['expected_sections']}"
    )
    surfaced = {result["metadata"]["section"] for result in results}
    for section in entry["expected_sections"]:
        assert section in surfaced, (
            f"query {entry['query']!r} did not surface {section!r}; surfaced {sorted(surfaced)}"
        )


@pytest.mark.asyncio
async def test_metadata_filter_restricts_to_matching_section(
    tierb_seed: dict[str, Any],
):
    """An equality filter on a metadata column scopes both search stages."""
    store = get_vector_store(table_name="policy_chunks", id_field="id")
    results = await store.hybrid_search(
        query="water damage",
        embedding=hash_embedding("water damage"),
        n_results=5,
        threshold=DISTANCE_THRESHOLD,
        text_field="text",
        metadata_fields=["section", "chunk_id"],
        section="Section 1: Home Water Damage Coverage",
    )
    assert results
    for row in results:
        assert row["metadata"]["section"] == "Section 1: Home Water Damage Coverage"


@pytest.mark.asyncio
async def test_retrieval_metrics_match_baseline(tierb_seed: dict[str, Any]):
    """Recall@5, precision@5, MRR, and p50 latency stay within the baseline.

    The first run records ``backend/tests/baselines/rag_baseline.json``;
    every later run asserts the deterministic metrics (recall, precision,
    MRR) within +/-10% of it. Wall-clock latency cannot hold a 10% band on
    a shared machine, so it is guarded against catastrophic drift (missing
    index, full scan) with a 2x + 25ms upper bound instead.
    """
    matrix = _load_matrix()
    chunks_by_section = await section_chunk_ids()

    recalls: list[float] = []
    precisions: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies_ms: list[float] = []
    per_query: dict[str, dict[str, float]] = {}

    for entry in matrix["queries"]:
        relevant: list[str] = []
        for section in entry["expected_sections"]:
            relevant.extend(chunks_by_section.get(section, []))
        relevant_set = set(relevant)

        started = time.perf_counter()
        results = await retrieve_hybrid(entry["query"], n_results=5)
        latencies_ms.append((time.perf_counter() - started) * 1000.0)

        retrieved: list[str] = []
        for result in results:
            retrieved.extend(chunks_by_section.get(result["metadata"]["section"], []))
        retrieved_set = set(retrieved)

        hits = len(retrieved_set & relevant_set)
        recall = hits / len(relevant_set) if relevant_set else (1.0 if not retrieved_set else 0.0)
        precision = hits / len(retrieved_set) if retrieved_set else 1.0

        reciprocal_rank = 0.0
        for index, chunk_id in enumerate(retrieved):
            if chunk_id in relevant_set:
                reciprocal_rank = 1.0 / (index + 1)
                break

        recalls.append(recall)
        precisions.append(precision)
        reciprocal_ranks.append(reciprocal_rank)
        per_query[entry["id"]] = {
            "recall": recall,
            "precision": precision,
            "reciprocal_rank": reciprocal_rank,
        }

    metrics = {
        "recall_at_5": sum(recalls) / len(recalls),
        "precision_at_5": sum(precisions) / len(precisions),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "p50_latency_ms": _percentile(latencies_ms, 0.5),
        "query_count": len(matrix["queries"]),
        "per_query": per_query,
    }

    if not BASELINE_PATH.exists():
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    for key in ("recall_at_5", "precision_at_5", "mrr"):
        assert abs(metrics[key] - baseline[key]) <= 0.10 * abs(baseline[key]) + 1e-9, (
            f"{key} drifted: {metrics[key]:.4f} vs baseline {baseline[key]:.4f}"
        )
    assert metrics["p50_latency_ms"] <= baseline["p50_latency_ms"] * 2.0 + 25.0, (
        f"p50 latency {metrics['p50_latency_ms']:.1f}ms exceeds the drift "
        f"bound for baseline {baseline['p50_latency_ms']:.1f}ms"
    )
