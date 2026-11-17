from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import Field

from omnisearch.domain.enums import EvidenceLevel
from omnisearch.domain.models import StrictModel


class EvidenceStance(StrEnum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"
    CONTEXT = "context"


class EvidenceAssessment(StrictModel):
    level: EvidenceLevel
    score: float = Field(ge=0, le=1)
    reasons: list[str]


class EvidenceItem(StrictModel):
    source_id: str
    result_type: str
    url: str
    stance: EvidenceStance = EvidenceStance.CONTEXT
    content_length: int = Field(default=0, ge=0)


class ClaimAssessment(StrictModel):
    claim: str
    status: str
    confidence: float = Field(ge=0, le=1)
    supporting_sources: int = Field(ge=0)
    opposing_sources: int = Field(ge=0)
    evidence: list[EvidenceAssessment]
    limitations: list[str]


class EvidenceEvaluator:
    _primary_sources = {"arxiv", "github", "gitlab", "core"}
    _research_indexes = {"crossref", "openalex", "semantic_scholar", "google_scholar"}
    _community_sources = {"reddit", "stack_exchange"}

    def evaluate(self, item: EvidenceItem) -> EvidenceAssessment:
        reasons: list[str] = []
        if item.source_id in self._primary_sources and item.result_type in {
            "paper",
            "repository",
        }:
            level, score = EvidenceLevel.E1, 0.9
            reasons.append("原始论文、全文索引或原始代码仓库")
        elif item.source_id in self._research_indexes:
            level, score = EvidenceLevel.E2, 0.8
            reasons.append("可信学术索引或研究聚合来源")
        elif item.source_id in self._community_sources:
            level, score = EvidenceLevel.E4, 0.45
            reasons.append("社区经验仅作为线索")
        elif self._looks_official(item.url):
            level, score = EvidenceLevel.E1, 0.88
            reasons.append("政府、标准组织或官方文档域名")
        elif item.source_id in {"google", "duckduckgo", "brave"}:
            level, score = EvidenceLevel.E3, 0.62
            reasons.append("综合搜索发现的网页，仍需核对原始出处")
        else:
            level, score = EvidenceLevel.E5, 0.25
            reasons.append("来源类型尚未充分验证")
        if item.content_length >= 1000:
            score = min(1.0, score + 0.05)
            reasons.append("已获取足量正文，可进行段落级核验")
        elif item.content_length == 0:
            score = max(0.0, score - 0.1)
            reasons.append("当前只有检索元数据或摘要")
        return EvidenceAssessment(level=level, score=score, reasons=reasons)

    def verify_claim(self, claim: str, items: list[EvidenceItem]) -> ClaimAssessment:
        assessments = [self.evaluate(item) for item in items]
        supports = {
            urlsplit(item.url).netloc.lower()
            for item in items
            if item.stance is EvidenceStance.SUPPORTS
        }
        opposes = {
            urlsplit(item.url).netloc.lower()
            for item in items
            if item.stance is EvidenceStance.OPPOSES
        }
        support_scores = [
            assessment.score
            for item, assessment in zip(items, assessments, strict=True)
            if item.stance is EvidenceStance.SUPPORTS
        ]
        limitations: list[str] = []
        if opposes:
            status = "CONTESTED"
            limitations.append("存在反对或限制证据，不能表述为无条件事实")
        elif len(supports) >= 2 and support_scores:
            status = "SUPPORTED"
        elif support_scores:
            status = "PARTIALLY_SUPPORTED"
            limitations.append("缺少至少两个独立来源的交叉验证")
        else:
            status = "UNVERIFIED"
            limitations.append("没有明确标记为支持该结论的证据")
        confidence = sum(support_scores) / len(support_scores) if support_scores else 0.0
        if len(supports) >= 2:
            confidence = min(1.0, confidence + 0.05)
        if opposes:
            confidence *= 0.7
        return ClaimAssessment(
            claim=claim,
            status=status,
            confidence=confidence,
            supporting_sources=len(supports),
            opposing_sources=len(opposes),
            evidence=assessments,
            limitations=limitations,
        )

    @staticmethod
    def _looks_official(url: str) -> bool:
        hostname = (urlsplit(url).hostname or "").lower()
        return hostname.endswith((".gov", ".gov.cn", ".edu", ".ac.uk")) or hostname in {
            "www.w3.org",
            "www.iso.org",
            "docs.python.org",
            "developer.mozilla.org",
        }
