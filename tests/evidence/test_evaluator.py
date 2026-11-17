from omnisearch.core.evidence import EvidenceEvaluator, EvidenceItem
from omnisearch.domain.enums import EvidenceLevel


def test_rates_primary_code_above_community_metadata() -> None:
    evaluator = EvidenceEvaluator()
    repository = evaluator.evaluate(
        EvidenceItem(
            source_id="github",
            result_type="repository",
            url="https://github.com/org/project",
            content_length=3000,
        )
    )
    discussion = evaluator.evaluate(
        EvidenceItem(
            source_id="reddit",
            result_type="post",
            url="https://reddit.com/r/audio/comments/1",
        )
    )

    assert repository.level is EvidenceLevel.E1
    assert discussion.level is EvidenceLevel.E4
    assert repository.score > discussion.score


def test_cross_validates_claim_and_surfaces_conflict() -> None:
    assessment = EvidenceEvaluator().verify_claim(
        "The detector localizes a watermark",
        [
            EvidenceItem(
                source_id="arxiv",
                result_type="paper",
                url="https://arxiv.org/abs/1",
                stance="supports",
            ),
            EvidenceItem(
                source_id="github",
                result_type="repository",
                url="https://github.com/org/project",
                stance="supports",
            ),
            EvidenceItem(
                source_id="reddit",
                result_type="post",
                url="https://reddit.com/r/audio/comments/1",
                stance="opposes",
            ),
        ],
    )

    assert assessment.status == "CONTESTED"
    assert assessment.supporting_sources == 2
    assert assessment.opposing_sources == 1
    assert assessment.limitations
