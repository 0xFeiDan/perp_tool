from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.container_entrypoint import materialize_connection_urls


def test_component_passwords_are_url_encoded(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "strategy")
    monkeypatch.setenv("POSTGRES_USER", "user@name")
    monkeypatch.setenv("POSTGRES_PASSWORD", "a@b:c/d")
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("REDIS_PASSWORD", "r@d:s")

    materialize_connection_urls()

    database = urlsplit(os.environ["DATABASE_URL"])
    assert database.hostname == "postgres"
    assert database.username == "user%40name"
    assert database.password == "a%40b%3Ac%2Fd"
    assert os.environ["REDIS_URL"] == "redis://:r%40d%3As@redis:6379/0"
