import re

from omnisearch.domain.enums import SourceCategory


class HeuristicQueryAnalyzer:
    """Deterministic fallback used when no model gateway is configured."""

    _technical_terms = {
        "水印": "watermark",
        "音频": "audio",
        "音乐": "music",
        "检测": "detection",
        "定位": "localization",
        "开源": "open source",
        "项目": "project",
        "论文": "paper",
        "算法": "algorithm",
        "技术": "technology",
        "原理": "mechanism",
        "隐藏": "hidden",
        "生成": "generation",
        "人工智能": "artificial intelligence",
    }

    def detect_language(self, text: str) -> str:
        if re.search(r"[\u4e00-\u9fff]", text):
            return "zh"
        if re.search(r"[\u3040-\u30ff]", text):
            return "ja"
        return "en"

    def categories(self, text: str) -> set[SourceCategory]:
        lowered = text.lower()
        categories = {SourceCategory.WEB}
        if any(term in lowered for term in ("github", "开源", "代码", "repository", "source code")):
            categories.add(SourceCategory.CODE)
        if any(
            term in lowered
            for term in (
                "水印",
                "检测",
                "定位",
                "论文",
                "研究",
                "算法",
                "学术",
                "watermark",
                "detection",
                "localization",
                "paper",
                "research",
                "algorithm",
            )
        ):
            categories.add(SourceCategory.ACADEMIC)
        if any(term in lowered for term in ("社区", "讨论", "经验", "reddit", "forum")):
            categories.add(SourceCategory.COMMUNITY)
        if any(term in lowered for term in ("专利", "patent")):
            categories.add(SourceCategory.PATENT)
        return categories

    def english_expansion(self, text: str) -> str | None:
        if self.detect_language(text) == "en":
            return text
        entities = re.findall(r"[A-Za-z][A-Za-z0-9._+-]*(?:\s+[A-Za-z0-9._+-]+)*", text)
        terms = [english for original, english in self._technical_terms.items() if original in text]
        combined = list(dict.fromkeys([*entities, *terms]))
        return " ".join(combined) if combined else None
