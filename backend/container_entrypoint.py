"""Build secret-safe DSNs inside the container before starting a command.

Docker Compose passes PostgreSQL/Redis components as separate environment
variables.  This avoids interpolating random passwords into a URL in YAML,
where characters such as ``@`` and ``:`` would otherwise change its meaning.
Nothing is printed by this module.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import quote


def _required(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def materialize_connection_urls() -> None:
    if not os.getenv("DATABASE_URL"):
        host = _required("POSTGRES_HOST")
        database = _required("POSTGRES_DB")
        username = _required("POSTGRES_USER")
        password = _required("POSTGRES_PASSWORD")
        port = os.getenv("POSTGRES_PORT", "5432")
        if all((host, database, username, password)):
            os.environ["DATABASE_URL"] = (
                "postgresql+psycopg://"
                f"{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(database, safe='')}"
            )

    if not os.getenv("REDIS_URL"):
        host = _required("REDIS_HOST")
        password = _required("REDIS_PASSWORD")
        port = os.getenv("REDIS_PORT", "6379")
        if host and password:
            os.environ["REDIS_URL"] = f"redis://:{quote(password, safe='')}@{host}:{port}/0"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("container entrypoint requires a command")
    materialize_connection_urls()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
