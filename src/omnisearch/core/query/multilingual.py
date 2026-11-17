import re

from omnisearch.core.query.heuristic import HeuristicQueryAnalyzer


class MultilingualQueryEngine:
    """Builds native terminology variants while preserving named entities."""

    _terms = {
        "ja": {
            "水印": "音声電子透かし",
            "音频": "音声",
            "音乐": "音楽",
            "检测": "検出",
            "定位": "位置特定",
            "开源": "オープンソース",
            "论文": "論文",
            "算法": "アルゴリズム",
            "原理": "仕組み",
        },
        "de": {
            "水印": "Audio-Wasserzeichen",
            "音频": "Audio",
            "音乐": "Musik",
            "检测": "Erkennung",
            "定位": "Lokalisierung",
            "开源": "Open Source",
            "论文": "Forschungsarbeit",
            "算法": "Algorithmus",
            "原理": "Funktionsweise",
        },
        "ko": {
            "水印": "오디오 워터마크",
            "音频": "오디오",
            "音乐": "음악",
            "检测": "탐지",
            "定位": "위치 추정",
            "开源": "오픈 소스",
            "论文": "논문",
            "算法": "알고리즘",
            "原理": "원리",
        },
    }

    def __init__(self, analyzer: HeuristicQueryAnalyzer | None = None) -> None:
        self.analyzer = analyzer or HeuristicQueryAnalyzer()

    def expand(self, question: str, original_language: str, limit: int) -> list[tuple[str, str]]:
        variants = [(original_language, question)]
        english = self.analyzer.english_expansion(question)
        if original_language != "en" and english:
            variants.append(("en", english))
        domain_languages = self._domain_languages(question)
        for language in domain_languages:
            if language == original_language or len(variants) >= limit:
                continue
            expanded = self._native_variant(question, language)
            if expanded:
                variants.append((language, expanded))
        return variants[:limit]

    @staticmethod
    def _domain_languages(question: str) -> list[str]:
        lowered = question.lower()
        if any(term in lowered for term in ("audio", "music", "音频", "音乐", "水印")):
            return ["ja", "de", "ko"]
        if any(term in lowered for term in ("ai", "人工智能", "模型")):
            return ["ja", "ko", "de"]
        return ["ja", "de"]

    def _native_variant(self, question: str, language: str) -> str | None:
        entities = re.findall(r"[A-Za-z][A-Za-z0-9._+-]*", question)
        translated = [
            target for source, target in self._terms.get(language, {}).items() if source in question
        ]
        terms = list(dict.fromkeys([*entities, *translated]))
        return " ".join(terms) if terms else None
