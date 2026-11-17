from enum import StrEnum


class ResearchStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SearchJobStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    NAVIGATING = "navigating"
    LOADING = "loading"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    BROWSER_CRASHED = "browser_crashed"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    PARSE_FAILED = "parse_failed"
    CANCELLED = "cancelled"


class Channel(StrEnum):
    API = "api"
    BROWSER = "browser"


class SourceCategory(StrEnum):
    WEB = "web"
    ACADEMIC = "academic"
    CODE = "code"
    DOCUMENTATION = "documentation"
    COMMUNITY = "community"
    PATENT = "patent"


class SourceRole(StrEnum):
    DISCOVERY = "discovery"
    PRIMARY_INDEX = "primary_index"
    CODE_HOST = "code_host"
    COMMUNITY = "community"
    SPECIALIST = "specialist"


class EvidenceLevel(StrEnum):
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"
