from uuid import UUID, uuid4

from pydantic import Field

from omnisearch.core.query import HeuristicQueryAnalyzer, MultilingualQueryEngine
from omnisearch.domain.enums import Channel, SourceCategory
from omnisearch.domain.models import ResearchBudget, StrictModel
from omnisearch.models import ModelResearchAnalysis, ResearchModelGateway
from omnisearch.providers.registry import SourceRegistry


class PlannedQuery(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    sub_question_id: UUID
    language: str
    query: str
    sources: list[str]
    purpose: str


class PlannedSubQuestion(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    objective: str
    categories: set[SourceCategory]
    evidence_requirements: list[str]
    depends_on: list[UUID] = Field(default_factory=list)


class ResearchPlan(StrictModel):
    question: str
    original_language: str
    sub_questions: list[PlannedSubQuestion]
    queries: list[PlannedQuery]
    budget: ResearchBudget


class SearchPlanner:
    def __init__(
        self,
        registry: SourceRegistry,
        analyzer: HeuristicQueryAnalyzer | None = None,
        multilingual: MultilingualQueryEngine | None = None,
        model_gateway: ResearchModelGateway | None = None,
        available_sources: set[str] | None = None,
        source_channels: dict[str, Channel] | None = None,
        require_dual_channel: bool = False,
    ) -> None:
        self.registry = registry
        self.analyzer = analyzer or HeuristicQueryAnalyzer()
        self.multilingual = multilingual or MultilingualQueryEngine(self.analyzer)
        self.model_gateway = model_gateway
        self.available_sources = available_sources
        self.source_channels = source_channels or {
            source.source_id: source.adapters[0].channel
            for source in registry.list(enabled_only=False)
            if source.adapters
        }
        self.require_dual_channel = require_dual_channel

    async def plan_async(
        self, question: str, budget: ResearchBudget | None = None
    ) -> ResearchPlan:
        plan = self.plan(question, budget)
        if self.model_gateway is None:
            return plan
        try:
            analysis = await self.model_gateway.analyze(question)
        except Exception:
            return plan
        return self._finalize(self._augment_with_model(plan, analysis))

    def plan(self, question: str, budget: ResearchBudget | None = None) -> ResearchPlan:
        budget = budget or ResearchBudget()
        language = self.analyzer.detect_language(question)
        requested = self.analyzer.categories(question)
        sub_questions = self._sub_questions(question, requested)[: budget.max_sub_questions]
        queries: list[PlannedQuery] = []
        variants = self.multilingual.expand(question, language, budget.max_languages)
        sources_by_question: dict[UUID, list[str]] = {}
        for sub_question in sub_questions:
            sources_by_question[sub_question.id] = [
                source.source_id
                for source in self.registry.select(
                    sub_question.categories,
                    limit=budget.max_sources_per_query,
                )
                if self.available_sources is None
                or source.source_id in self.available_sources
            ]
        sub_questions = [
            question for question in sub_questions if sources_by_question[question.id]
        ]
        for variant_language, variant_query in variants:
            for sub_question in sub_questions:
                if len(queries) >= budget.max_queries:
                    break
                queries.append(
                    PlannedQuery(
                        sub_question_id=sub_question.id,
                        language=variant_language,
                        query=self._query_for(sub_question, variant_query),
                        sources=sources_by_question[sub_question.id],
                        purpose=(
                            "original-language discovery"
                            if variant_language == language
                            else "native terminology and primary-source discovery"
                        ),
                    )
                )
            if len(queries) >= budget.max_queries:
                break
        executable_ids = {query.sub_question_id for query in queries}
        sub_questions = [
            question for question in sub_questions if question.id in executable_ids
        ]
        plan = ResearchPlan(
            question=question,
            original_language=language,
            sub_questions=sub_questions,
            queries=queries,
            budget=budget,
        )
        return self._finalize(plan)

    def _finalize(self, plan: ResearchPlan) -> ResearchPlan:
        return self.ensure_dual_channel(plan) if self.require_dual_channel else plan

    def channel_coverage(self, plan: ResearchPlan) -> set[Channel]:
        return {
            self.source_channels[source]
            for query in plan.queries
            for source in query.sources
            if source in self.source_channels
        }

    def ensure_dual_channel(self, plan: ResearchPlan) -> ResearchPlan:
        """Guarantee that a research plan uses both API and direct-browser discovery."""
        required = {Channel.API, Channel.BROWSER}
        missing = required - self.channel_coverage(plan)
        if not missing:
            return plan

        sub_questions = {item.id: item for item in plan.sub_questions}
        allowed = self.available_sources
        for channel in sorted(missing, key=lambda item: item.value):
            candidates = [
                source
                for source in self.registry.list(channel=channel)
                if self.source_channels.get(source.source_id) is channel
                and (allowed is None or source.source_id in allowed)
            ]
            if not candidates:
                raise ValueError(f"no configured {channel.value} source is available")

            placed = False
            for query in plan.queries:
                question = sub_questions[query.sub_question_id]
                candidate = next(
                    (
                        source
                        for source in candidates
                        if source.category in question.categories
                        and source.source_id not in query.sources
                    ),
                    None,
                )
                if candidate is None:
                    continue
                if len(query.sources) < plan.budget.max_sources_per_query:
                    query.sources.append(candidate.source_id)
                    query.purpose += f"; required {channel.value} corroboration"
                    placed = True
                    break

            if not placed and len(plan.queries) < plan.budget.max_queries:
                candidate = candidates[0]
                target = next(
                    (
                        item
                        for item in plan.sub_questions
                        if candidate.category in item.categories
                    ),
                    plan.sub_questions[0],
                )
                plan.queries.append(
                    PlannedQuery(
                        sub_question_id=target.id,
                        language=plan.original_language,
                        query=self._query_for(target, plan.question),
                        sources=[candidate.source_id],
                        purpose=f"required {channel.value} independent corroboration",
                    )
                )
                placed = True

            if not placed:
                raise ValueError(
                    f"research budget cannot fit required {channel.value} coverage"
                )
        return plan

    @staticmethod
    def _query_for(sub_question: PlannedSubQuestion, question: str) -> str:
        category_terms = {
            SourceCategory.ACADEMIC: "paper research",
            SourceCategory.CODE: "open source GitHub implementation",
            SourceCategory.PATENT: "patent",
            SourceCategory.COMMUNITY: "discussion limitations",
            SourceCategory.WEB: "official technical documentation",
        }
        suffix = " ".join(
            category_terms[item]
            for item in sorted(sub_question.categories, key=lambda category: category.value)
        )
        return f"{question} {suffix}".strip()

    @staticmethod
    def _sub_questions(
        question: str, requested: set[SourceCategory]
    ) -> list[PlannedSubQuestion]:
        lowered = question.lower()
        primary = PlannedSubQuestion(
            objective=f"确认问题中的核心技术、实体和官方公开信息：{question}",
            categories={SourceCategory.WEB},
            evidence_requirements=["官方文档或原始公开资料", "明确发布日期"],
        )
        questions = [primary]
        if SourceCategory.ACADEMIC in requested:
            questions.append(
                PlannedSubQuestion(
                    objective="调查相关论文、正式术语、方法和实验结果",
                    categories={SourceCategory.ACADEMIC},
                    evidence_requirements=["论文元数据", "原始论文或预印本"],
                    depends_on=[primary.id],
                )
            )
        if SourceCategory.CODE in requested:
            questions.append(
                PlannedSubQuestion(
                    objective="寻找可运行的开源实现并确认其实际能力与维护状态",
                    categories={SourceCategory.CODE},
                    evidence_requirements=["原始代码仓库", "许可证和近期活动"],
                    depends_on=[primary.id],
                )
            )
        if any(term in lowered for term in ("检测", "detect", "识别")):
            questions.append(
                PlannedSubQuestion(
                    objective="调查检测方法、输入要求、置信度指标和误报漏报条件",
                    categories={SourceCategory.ACADEMIC},
                    evidence_requirements=["检测算法原文", "定量实验指标"],
                    depends_on=[primary.id],
                )
            )
        if any(term in lowered for term in ("定位", "localiz", "位置")):
            questions.append(
                PlannedSubQuestion(
                    objective="调查水印的时间与频率定位能力、分辨率和局部置信度",
                    categories={SourceCategory.ACADEMIC, SourceCategory.CODE},
                    evidence_requirements=["定位方法原文", "可复现实验或代码"],
                    depends_on=[primary.id],
                )
            )
        questions.append(
            PlannedSubQuestion(
                objective="寻找反例、限制、失败条件和相互冲突的说法",
                categories={SourceCategory.WEB, SourceCategory.ACADEMIC},
                evidence_requirements=["至少两个独立来源", "限制或反对证据"],
                depends_on=[primary.id],
            )
        )
        if SourceCategory.PATENT in requested:
            questions.append(
                PlannedSubQuestion(
                    objective="调查相关专利、申请人和引用关系",
                    categories={SourceCategory.PATENT},
                    evidence_requirements=["专利原文或权威索引"],
                    depends_on=[primary.id],
                )
            )
        return questions

    def _augment_with_model(
        self, plan: ResearchPlan, analysis: ModelResearchAnalysis
    ) -> ResearchPlan:
        known = {" ".join(item.objective.lower().split()) for item in plan.sub_questions}
        remaining_questions = plan.budget.max_sub_questions - len(plan.sub_questions)
        remaining_queries = plan.budget.max_queries - len(plan.queries)
        variants = self.multilingual.expand(
            plan.question, plan.original_language, plan.budget.max_languages
        )
        for proposal in analysis.sub_questions[: max(0, remaining_questions)]:
            normalized = " ".join(proposal.objective.lower().split())
            if normalized in known or remaining_queries <= 0:
                continue
            sub_question = PlannedSubQuestion(
                objective=proposal.objective,
                categories=proposal.categories,
                evidence_requirements=proposal.evidence_requirements,
            )
            sources = [
                source.source_id
                for source in self.registry.select(
                    proposal.categories,
                    limit=plan.budget.max_sources_per_query,
                )
                if self.available_sources is None
                or source.source_id in self.available_sources
            ]
            if not sources:
                continue
            plan.sub_questions.append(sub_question)
            for language, variant in variants[:remaining_queries]:
                plan.queries.append(
                    PlannedQuery(
                        sub_question_id=sub_question.id,
                        language=language,
                        query=f"{variant} {proposal.objective}".strip(),
                        sources=sources,
                        purpose="model-decomposed research objective",
                    )
                )
                remaining_queries -= 1
                if remaining_queries <= 0:
                    break
            known.add(normalized)
        return plan
