import re
from collections import Counter

from omnisearch.core.planning import PlannedQuery, ResearchPlan
from omnisearch.services.search import QuickSearchResponse


class GapAnalyzer:
    _stopwords = {
        "about",
        "audio",
        "documentation",
        "evidence",
        "github",
        "hidden",
        "implementation",
        "official",
        "open",
        "paper",
        "research",
        "source",
        "technical",
        "watermark",
        "with",
    }

    def next_queries(
        self,
        plan: ResearchPlan,
        responses: list[tuple[PlannedQuery, QuickSearchResponse]],
        *,
        remaining: int,
    ) -> list[PlannedQuery]:
        if remaining <= 0:
            return []
        by_sub_question: dict = {}
        used = {self._normalized(query.query) for query in plan.queries}
        for query, response in responses:
            by_sub_question.setdefault(query.sub_question_id, []).append(response)
        generated: list[PlannedQuery] = []
        for sub_question in plan.sub_questions:
            collected = by_sub_question.get(sub_question.id, [])
            independent_sources = {
                occurrence.source_id
                for response in collected
                for result in response.results
                for occurrence in result.occurrences
            }
            if len(independent_sources) >= 2:
                continue
            terms = self._discovered_terms(collected)
            suffix = " ".join(terms[:3]) or "primary evidence verification"
            candidate = f"{sub_question.objective} {suffix}".strip()
            if self._normalized(candidate) in used:
                continue
            sources = list(
                dict.fromkeys(
                    source
                    for query in plan.queries
                    if query.sub_question_id == sub_question.id
                    for source in query.sources
                )
            )
            if not sources:
                continue
            generated.append(
                PlannedQuery(
                    sub_question_id=sub_question.id,
                    language=plan.original_language,
                    query=candidate,
                    sources=sources,
                    purpose="gap-driven corroboration and missing-evidence search",
                )
            )
            used.add(self._normalized(candidate))
            if len(generated) >= remaining:
                break
        return generated

    def _discovered_terms(self, responses: list[QuickSearchResponse]) -> list[str]:
        words = Counter(
            word.lower()
            for response in responses
            for result in response.results
            for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", result.primary.title)
            if word.lower() not in self._stopwords
        )
        return [word for word, _count in words.most_common(8)]

    @staticmethod
    def _normalized(query: str) -> str:
        return " ".join(query.lower().split())
