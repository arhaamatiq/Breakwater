"""Config parsing; no services needed."""
from breakwater.config import Config


def test_defaults(monkeypatch):
    for k in ("BW_ARTIFACTS", "BW_LEASE_SECONDS", "BW_MAX_ATTEMPTS", "BW_DSN"):
        monkeypatch.delenv(k, raising=False)
    c = Config()
    assert c.artifacts == "file://~/.breakwater/artifacts"
    assert c.dsn == "postgresql://breakwater:breakwater@127.0.0.1:55432/breakwater"
    assert (c.lease_seconds, c.max_attempts) == (6.0, 5)
    assert c.heartbeat_seconds < c.lease_seconds


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("BW_ARTIFACTS", "s3://bucket")
    monkeypatch.setenv("BW_LEASE_SECONDS", "1.5")
    monkeypatch.setenv("BW_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("BW_API_PORT", "9999")
    c = Config()
    assert (c.artifacts, c.lease_seconds, c.max_attempts) == ("s3://bucket", 1.5, 9)
    assert c.api_url == "http://127.0.0.1:9999"
