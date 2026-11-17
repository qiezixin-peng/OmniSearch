from omnisearch.quality import evaluate_quality


def test_computes_research_search_quality_metrics() -> None:
    evaluation = evaluate_quality(
        ["paper:a", "repo:b", "paper:a", "page:c"],
        {"paper:a", "repo:b", "paper:missing"},
        source_ids=["arxiv", "github", "openalex", "google"],
        languages=["en", "en", "zh", "ja"],
        primary_flags=[True, True, False, False],
        citation_checks=[True, True, False],
        k=3,
    )

    assert evaluation.precision_at_k == 1
    assert evaluation.recall_at_k == 2 / 3
    assert evaluation.source_diversity == 3
    assert evaluation.language_diversity == 2
    assert evaluation.duplicate_rate == 0.25
    assert evaluation.primary_source_rate == 2 / 3
    assert evaluation.citation_accuracy == 2 / 3
