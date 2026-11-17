from fastapi.testclient import TestClient

from omnisearch.apps.api.main import app


def test_api_research_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OMNISEARCH_DATABASE_URL", f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    )
    from omnisearch.config import get_settings

    get_settings.cache_clear()
    with TestClient(app) as client:
        created = client.post(
            "/v1/research",
            json={"question": "调查 AI 音频水印检测与定位", "original_language": "zh-CN"},
        )
        assert created.status_code == 201
        research_id = created.json()["id"]
        assert client.post(f"/v1/research/{research_id}/plan").status_code == 200
        assert client.post(f"/v1/research/{research_id}/start").status_code == 200
        completed = client.post(f"/v1/research/{research_id}/complete")
        assert completed.json()["status"] == "completed"
        events = client.get(f"/v1/research/{research_id}/events").json()
        assert len(events) == 4


def test_api_exposes_agent_search_pool(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OMNISEARCH_DATABASE_URL", f"sqlite:///{(tmp_path / 'sources.db').as_posix()}"
    )
    from omnisearch.config import get_settings

    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.get("/v1/sources", params={"category": "academic"})
        assert response.status_code == 200
        source_ids = {source["source_id"] for source in response.json()}
        assert {"arxiv", "crossref", "openalex"} <= source_ids

        google = client.get("/v1/sources/google")
        assert google.status_code == 200
        assert {item["backend"] for item in google.json()["adapters"]} == {
            "scrapling_chrome",
            "ego_browser",
        }


def test_api_key_protects_non_health_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OMNISEARCH_DATABASE_URL", f"sqlite:///{(tmp_path / 'auth.db').as_posix()}"
    )
    monkeypatch.setenv("OMNISEARCH_SERVICE_API_KEY", "test-secret")
    from omnisearch.config import get_settings

    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/sources").status_code == 401
        response = client.get(
            "/v1/sources", headers={"Authorization": "Bearer test-secret"}
        )
        assert response.status_code == 200
    get_settings.cache_clear()
