from pathlib import Path
from uuid import uuid4

from omnisearch.domain.enums import Channel
from omnisearch.domain.models import SearchResult
from omnisearch.quality.benchmark import BenchmarkCase, _responses_to_metrics, load_suite
from omnisearch.services.search import MergedSearchResult, QuickSearchResponse


def test_load_smoke_suite() -> None:
    suite = load_suite(Path("benchmarks/smoke.json"))
    assert suite.name == "OmniSearch smoke benchmark"
    assert "arxiv:1706.03762" in suite.cases[0].relevant_entities[0]


def test_response_evaluation_deduplicates_across_queries() -> None:
    case = BenchmarkCase(
        case_id="paper",
        question="Find a paper",
        relevant_entities=[
            ["arxiv:1706.03762", "https://arxiv.org/pdf/1706.03762"]
        ],
        queries={"en": ["paper"]},
        sources=["arxiv"],
    )
    result = SearchResult(
        id=uuid4(),
        source_id="arxiv",
        adapter_id="arxiv_api",
        channel=Channel.API,
        result_type="paper",
        title="Attention Is All You Need",
        url="https://arxiv.org/abs/1706.03762",
        original_language="en",
    )
    response = QuickSearchResponse(
        query="paper",
        results=[
            MergedSearchResult(
                canonical_key="arxiv:1706.03762", primary=result, occurrences=[result]
            )
        ],
        failures=[],
    )
    report = _responses_to_metrics(case, [response, response], 0.1, "multilingual")
    assert report.retrieved_keys == ["arxiv:1706.03762"]
    assert report.metrics.precision_at_k == 1
    assert report.metrics.recall_at_k == 1


def test_entity_aliases_count_as_one_relevant_item() -> None:
    case = BenchmarkCase(
        case_id="aliases",
        question="Find a paper",
        relevant_entities=[
            ["arxiv:1706.03762", "https://arxiv.org/pdf/1706.03762"]
        ],
        queries={"en": ["paper"]},
        sources=["arxiv"],
    )
    keys, relevant = case.normalize_keys(["https://arxiv.org/pdf/1706.03762"])
    assert keys == ["entity:aliases:0"]
    assert relevant == {"entity:aliases:0"}
