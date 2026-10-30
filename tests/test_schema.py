"""The schema: shipped as package data, idempotent by construction, and able to
upgrade a v1 database in place. The lint tests need no services; the upgrade
test needs Postgres (see conftest.py) and is skipped without it."""
import asyncio
import pathlib
import re
import uuid
from importlib import resources

from breakwater import db
from breakwater.report import verify_invariants

from conftest import create_database, drop_database

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEASE = 30.0


def test_schema_ships_as_package_data():
    sql = db.schema_sql()
    assert sql == resources.files("breakwater").joinpath("schema.sql").read_text()
    assert not (ROOT / "db" / "schema.sql").exists()   # one source of truth


def test_schema_is_idempotent_by_construction():
    """Every statement must be safe to re-run on a live database: that is what
    lets `breakwater.setup` double as the migration."""
    sql = re.sub(r"--[^\n]*", "", db.schema_sql())
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    for s in statements:
        head = " ".join(s.split()[:8]).upper()
        assert (head.startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS",
                                 "DROP INDEX IF EXISTS"))
                or re.match(r"ALTER TABLE \w+ ADD COLUMN IF NOT EXISTS", head)), s[:80]


def test_every_column_added_after_v1_has_an_upgrade_statement():
    """A column in CREATE TABLE but missing from the ADD COLUMN IF NOT EXISTS list
    would exist on fresh installs and be silently absent on upgraded ones."""
    v1 = (ROOT / "tests" / "fixtures" / "schema_v1.sql").read_text()
    sql = db.schema_sql()

    def columns(text):
        out = {}
        for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", text, re.S):
            body = re.sub(r"--[^\n]*", "", body)
            out[table] = {line.split()[0] for line in body.splitlines()
                          if line.strip() and line.split()[0].islower()
                          and not line.strip().startswith(("CHECK", "PRIMARY"))}
        return out
    upgrades = set(re.findall(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", sql))
    old, new = columns(v1), columns(sql)
    assert set(old) == set(new)
    added = {(t, c) for t in new for c in new[t] - old[t]}
    assert added and added == upgrades


def test_packaged_compose_matches_repo_compose():
    packaged = resources.files("breakwater").joinpath("compose.yml").read_text()
    assert packaged == (ROOT / "docker-compose.yml").read_text()


async def submit(pool, row, worker_id: str) -> bool:
    return await db.submit_result(
        pool, trial_id=row["trial_id"], experiment_id=row["experiment_id"],
        task_id=row["task_id"], outcome="pass", detail={}, exec_ms=1.0, worker_id=worker_id,
        attempt=row["attempts"], lease_epoch=row["lease_epoch"], artifacts=[])


V1_SCHEMA = (ROOT / "tests" / "fixtures" / "schema_v1.sql").read_text()

COLUMNS_SQL = """SELECT table_name, column_name, data_type, is_nullable, column_default
                 FROM information_schema.columns WHERE table_schema='public'
                 ORDER BY table_name, column_name"""
INDEXES_SQL = "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY 1"


def test_v1_database_upgrades_in_place(pg):
    """A database created by the v1 schema, with live rows, upgraded by re-applying
    the packaged schema (twice): identical shape to a fresh install, rows kept,
    and its queued work claimable under the new protocol."""
    name = f"bw_test_mig_{uuid.uuid4().hex[:8]}"

    async def main():
        dsn = await create_database(name, schema=V1_SCHEMA)
        try:
            pool = await db.make_pool(dsn)
            await pool.execute("INSERT INTO experiments (experiment_id, name) VALUES ('old','old')")
            await pool.execute("""INSERT INTO trials (trial_id, experiment_id, task_id, state)
                                  VALUES ('old/0','old','t','queued'), ('old/1','old','t','completed')""")
            await pool.execute("""INSERT INTO results (trial_id, experiment_id, task_id, outcome,
                                  worker_id, attempt) VALUES ('old/1','old','t','pass','w',1)""")
            await pool.execute("""INSERT INTO events (experiment_id, trial_id, worker_id, kind, payload)
                                  VALUES ('old','old/1','w','claimed','{"attempt": 1}'),
                                         ('old','old/1','w','result_accepted','{"attempt": 1}')""")
            async with pool.acquire() as conn:
                await db.apply_schema(conn)
                await db.apply_schema(conn)
            fresh = await db.make_pool(pg)
            try:
                assert [tuple(r) for r in await pool.fetch(COLUMNS_SQL)] == \
                       [tuple(r) for r in await fresh.fetch(COLUMNS_SQL)]
                assert [tuple(r) for r in await pool.fetch(INDEXES_SQL)] == \
                       [tuple(r) for r in await fresh.fetch(INDEXES_SQL)]
            finally:
                await fresh.close()
            r = await pool.fetchrow("SELECT lease_epoch, stale_submission FROM results")
            assert (r["lease_epoch"], r["stale_submission"]) == (None, False)
            row = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
            assert (row["trial_id"], row["lease_epoch"]) == ("old/0", 1)
            assert await submit(pool, row, "w1")
            assert (await verify_invariants(pool, "old"))["violations"] == {}
            await pool.close()
        finally:
            await drop_database(name)
    asyncio.run(main())
