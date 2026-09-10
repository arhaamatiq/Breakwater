"""One-shot infra setup: apply schema, create the artifact bucket, pull the trial image.
Run after `docker compose up -d`:  python -m projectx.setup
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
    storage.ensure_bucket()
    print(f"bucket '{CFG.s3_bucket}' ready")
    ensure_image()
    print(f"image '{CFG.docker_image}' ready")


if __name__ == "__main__":
    sys.exit(main())
