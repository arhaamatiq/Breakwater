"""Central config. Everything overridable via BW_* env vars."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    dsn: str = field(default_factory=lambda: os.environ.get(
        "BW_DSN", "postgresql://breakwater:breakwater@127.0.0.1:55432/breakwater"))
    s3_endpoint: str = field(default_factory=lambda: os.environ.get(
        "BW_S3_ENDPOINT", "http://127.0.0.1:59000"))
    s3_key: str = field(default_factory=lambda: os.environ.get("BW_S3_KEY", "breakwater"))
    s3_secret: str = field(default_factory=lambda: os.environ.get("BW_S3_SECRET", "breakwater123"))
    s3_bucket: str = field(default_factory=lambda: os.environ.get("BW_S3_BUCKET", "artifacts"))

    lease_seconds: float = field(default_factory=lambda: float(os.environ.get("BW_LEASE_SECONDS", "6")))
    heartbeat_seconds: float = field(default_factory=lambda: float(os.environ.get("BW_HEARTBEAT_SECONDS", "2")))
    reaper_interval: float = field(default_factory=lambda: float(os.environ.get("BW_REAPER_INTERVAL", "1")))
    worker_dead_after: float = field(default_factory=lambda: float(os.environ.get("BW_WORKER_DEAD_AFTER", "8")))

    worker_concurrency: int = field(default_factory=lambda: int(os.environ.get("BW_WORKER_CONCURRENCY", "3")))
    max_attempts: int = field(default_factory=lambda: int(os.environ.get("BW_MAX_ATTEMPTS", "5")))
    trial_timeout: float = field(default_factory=lambda: float(os.environ.get("BW_TRIAL_TIMEOUT", "60")))
    docker_image: str = field(default_factory=lambda: os.environ.get("BW_DOCKER_IMAGE", "python:3.11-slim"))

    api_host: str = field(default_factory=lambda: os.environ.get("BW_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: int(os.environ.get("BW_API_PORT", "8400")))

    @property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"


CFG = Config()
