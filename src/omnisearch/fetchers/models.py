from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, HttpUrl, model_validator

from omnisearch.domain.models import StrictModel, utc_now


class FetchMode(StrEnum):
    HTTP = "http"
    CHROME_MANAGED = "chrome_managed"
    CHROME_CDP = "chrome_cdp"
    EGO_BROWSER = "ego_browser"


class FetchRequest(StrictModel):
    url: HttpUrl
    mode: FetchMode = FetchMode.HTTP
    timeout_ms: int = Field(default=30_000, ge=1_000, le=180_000)
    max_bytes: int = Field(default=30 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    css_selector: str | None = Field(default=None, max_length=1000)
    main_content_only: bool = True
    locale: str | None = Field(default=None, max_length=35)

    # Browser execution and state isolation.
    chrome_user_data_dir: str | None = None
    cdp_url: str | None = None
    cdp_new_context: bool = True
    ego_task_space_id: int | None = Field(default=None, ge=1)
    ego_task_name: str = Field(default="OmniSearch acquisition", min_length=1, max_length=120)

    # Full Scrapling capabilities; the scheduler chooses these per job.
    solve_cloudflare: bool = False
    block_ads: bool = True
    disable_resources: bool = False
    network_idle: bool = False
    hide_canvas: bool = False
    block_webrtc: bool = False
    allow_webgl: bool = True
    pierce_shadow: bool = False
    proxy_secret_ref: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_mode_settings(self) -> "FetchRequest":
        if self.mode is FetchMode.CHROME_MANAGED and not self.chrome_user_data_dir:
            raise ValueError("managed Chrome requires a dedicated user data directory")
        if self.mode is FetchMode.CHROME_CDP and not self.cdp_url:
            raise ValueError("CDP Chrome requires an explicit authorized endpoint")
        return self


class FetchedDocument(StrictModel):
    url: HttpUrl
    final_url: HttpUrl
    mode: FetchMode
    status_code: int
    content_type: str | None = None
    markdown: str
    raw_html: str | None = None
    raw_bytes: bytes | None = None
    fetched_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
