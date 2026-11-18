from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMNISEARCH_", env_file=".env")

    env: str = "development"
    database_url: str = "sqlite:///./omnisearch.db"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    service_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    brave_search_api_key: SecretStr | None = None
    openalex_api_key: SecretStr | None = None
    semantic_scholar_api_key: SecretStr | None = None
    gitlab_token: SecretStr | None = None
    core_api_key: SecretStr | None = None
    x_bearer_token: SecretStr | None = None
    youtube_api_key: SecretStr | None = None
    rsshub_base_url: str = "http://127.0.0.1:1200"
    rsshub_routes: str = (
        "/google/news/Top%20stories/hl=en-US&gl=US&ceid=US:en,"
        "/reuters/world,/hackernews/index"
    )
    model_base_url: str | None = None
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None
    object_store_backend: str = "local"
    object_store_path: Path = Path(".runtime/objects")
    s3_endpoint_url: str | None = None
    s3_bucket: str = "omnisearch"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    runtime_dir: Path = Path(".runtime")
    chrome_user_data_dir: Path = Path(".runtime/chrome-profile")
    ego_script_dir: Path = Path(".runtime/ego-scripts")
    browser_headless: bool = True
    patchright_channel: str = "chrome"
    camoufox_enabled: bool = True
    camoufox_python: Path = Path(".venv-camoufox312/Scripts/python.exe")
    sticky_proxy_enabled: bool = True
    sticky_proxy_port: int = Field(default=17897, ge=1024, le=65535)
    sticky_proxy_controller_port: int = Field(default=19097, ge=1024, le=65535)
    clash_verge_config_dir: Path = Path(
        "~/AppData/Roaming/io.github.clash-verge-rev.clash-verge-rev"
    )
    mihomo_executable: Path = Path("C:/Program Files/Clash Verge/verge-mihomo.exe")
    search_api_timeout_seconds: float = Field(default=20.0, gt=0)
    search_browser_timeout_seconds: float = Field(default=35.0, gt=0)
    search_cache_ttl_seconds: float = Field(default=900.0, ge=0)
    search_cache_max_entries: int = Field(default=2048, ge=1, le=100000)
    research_query_concurrency: int = Field(default=8, ge=1, le=64)
    acquisition_concurrency: int = Field(default=6, ge=1, le=32)


@lru_cache
def get_settings() -> Settings:
    return Settings()
