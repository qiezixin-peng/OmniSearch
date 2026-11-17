import json
from typing import Protocol

import httpx
from pydantic import Field

from omnisearch.domain.enums import SourceCategory
from omnisearch.domain.models import StrictModel


class ProposedSubQuestion(StrictModel):
    objective: str = Field(min_length=3, max_length=1000)
    categories: set[SourceCategory] = Field(min_length=1)
    evidence_requirements: list[str] = Field(default_factory=list)


class ModelResearchAnalysis(StrictModel):
    canonical_terms: list[str] = Field(default_factory=list)
    suggested_languages: list[str] = Field(default_factory=list)
    sub_questions: list[ProposedSubQuestion] = Field(default_factory=list)


class ResearchModelGateway(Protocol):
    async def analyze(self, question: str) -> ModelResearchAnalysis: ...


class OpenAICompatibleModelGateway:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.client = client

    async def analyze(self, question: str) -> ModelResearchAnalysis:
        schema = ModelResearchAnalysis.model_json_schema()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You decompose research questions for a multilingual search engine. "
                        "Return only JSON matching this schema: " + json.dumps(schema)
                    ),
                },
                {"role": "user", "content": question},
            ],
        }
        if self.client is None:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                response = await client.post(self.endpoint, headers=headers, json=payload)
        else:
            response = await self.client.post(self.endpoint, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return ModelResearchAnalysis.model_validate_json(content)
