"""Settings read from environment variables, so the same code runs locally, in Docker and in CI."""

import os
from dataclasses import dataclass
from pathlib import Path

SAMPLE_DATA_DIR = Path(__file__).parent / "sample_data"


@dataclass(frozen=True)
class Settings:
    database_url: str
    # "mock" reads the XML files in app/sample_data; anything else is treated as a real OAI-PMH base URL.
    oai_base_url: str
    oai_metadata_prefix: str
    oai_set: str | None
    max_retries: int
    retry_backoff_seconds: float
    request_timeout_seconds: float


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./records.db"),
        oai_base_url=os.getenv("OAI_BASE_URL", "mock"),
        oai_metadata_prefix=os.getenv("OAI_METADATA_PREFIX", "oai_dc"),
        oai_set=os.getenv("OAI_SET") or None,
        max_retries=int(os.getenv("OAI_MAX_RETRIES", "3")),
        retry_backoff_seconds=float(os.getenv("OAI_RETRY_BACKOFF_SECONDS", "1.0")),
        request_timeout_seconds=float(os.getenv("OAI_TIMEOUT_SECONDS", "30")),
    )
