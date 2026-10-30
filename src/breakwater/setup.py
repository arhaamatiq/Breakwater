"""One-shot infra setup: apply schema, prepare the artifact store, pull the trial image.
Run after `docker compose up -d`:  python -m breakwater.setup

The artifact step creates a local directory by default; it only talks to
S3/MinIO when BW_ARTIFACTS is an s3:// location.
"""
import asyncio
import pathlib
import sys

import asyncpg

from .config import CFG
from . import storage
from .runner import ensure_image


async def apply_schema():
    schema = (pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    for attempt in range(30):
        try:
            conn = await asyncpg.connect(CFG.dsn)
            break
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(1)
    try:
        await conn.execute(schema)
    finally:
        await conn.close()
    print("schema applied")


def main():
    asyncio.run(apply_schema())
    print(f"artifact store ready: {storage.ensure_store()}")
    ensure_image()
    print(f"image '{CFG.docker_image}' ready")


if __name__ == "__main__":
    sys.exit(main())
