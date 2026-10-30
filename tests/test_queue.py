"""Integration tests for the Postgres work queue: claims, leases, fencing tokens,
fair scheduling, the result ledger, invariant checks, and schema migration.

Real Postgres (see conftest.py; skipped if unreachable), real concurrency where
concurrency is the point (many asyncio tasks over several connection pools),
no mocks. Where a test needs "the worker stalled past its lease" without
waiting, it moves lease_expires_at into the past; the lease-expiry test itself
uses a real short lease and real sleeping.
"""
import asyncio
from collections import Counter

from breakwater import db
from breakwater.config import CFG
from breakwater.report import compute_report, verify_invariants

LEASE = 30.0  # nothing expires unless a test makes it


def trials(exp: str, n: int) -> list[dict]:
    return [{"trial_id": f"{exp}/{i:04d}", "task_id": "t", "replicate": i, "spec": {}}
            for i in range(n)]


async def submit(pool, row, worker_id: str, epoch: int | None = None) -> bool:
    return await db.submit_result(
        pool, trial_id=row["trial_id"], experiment_id=row["experiment_id"],
        task_id=row["task_id"], outcome="pass", detail={}, exec_ms=1.0,
        worker_id=worker_id, attempt=row["attempts"],
        lease_epoch=row["lease_epoch"] if epoch is None else epoch, artifacts=[])


async def expire(pool, trial_id: str) -> None:
    await pool.execute("UPDATE trials SET lease_expires_at = now() - interval '1 second' "
                       "WHERE trial_id = $1", trial_id)


async def trial(pool, trial_id: str):
    return await pool.fetchrow("SELECT * FROM trials WHERE trial_id = $1", trial_id)


async def events(pool, trial_id: str, kind: str):
    return await pool.fetch("SELECT worker_id, payload FROM events WHERE trial_id = $1 "
                            "AND kind = $2 ORDER BY event_id", trial_id, kind)


def run(dsn, body, pools: int = 1, size: int = 8):
    """asyncio.run(body(*pools)) with pools opened and closed around it."""
    async def main():
        ps = [await db.make_pool(dsn, min_size=1, max_size=size) for _ in range(pools)]
        try:
            return await body(*ps)
        finally:
            for p in ps:
                await p.close()
    return asyncio.run(main())


async def assert_safe(pool, *experiment_ids):
    for e in experiment_ids:
        inv = await verify_invariants(pool, e)
        assert inv["violations"] == {}, (e, inv)


# ---------------------------------------------------------------- claims

def test_no_double_claims_under_concurrency(pg):
    """32 claimers on 4 pools drain 600 trials: every trial claimed exactly once."""
    async def body(*pools):
        for e in range(5):
            await db.create_experiment(pools[0], f"E{e}", "x", {}, trials(f"E{e}", 120))
        claimed = []

        async def claimer(i):
            while row := await db.claim_trial(pools[i % len(pools)], f"w{i}", lease_seconds=LEASE):
                claimed.append((row["trial_id"], row["lease_epoch"]))
                await asyncio.sleep(0)

        await asyncio.gather(*(claimer(i) for i in range(32)))
        ids = [t for t, _ in claimed]
        assert len(ids) == 600 and len(set(ids)) == 600
        assert {ep for _, ep in claimed} == {1}
        states = dict(await pools[0].fetch("SELECT state, count(*) FROM trials GROUP BY state"))
        assert states == {"running": 600}
        n_claim_events = await pools[0].fetchval("SELECT count(*) FROM events WHERE kind='claimed'")
        assert n_claim_events == 600
        await assert_safe(pools[0], *(f"E{e}" for e in range(5)))
    run(pg, body, pools=4)


def test_lease_expiry_reap_and_reclaim_by_another_worker(pg):
    async def body(pool):
        await db.create_experiment(pool, "L", "x", {}, trials("L", 2))
        a = await db.claim_trial(pool, "w1", lease_seconds=0.3)
        keep = await db.claim_trial(pool, "w3", lease_seconds=LEASE)
        assert await db.heartbeat_trial(pool, a["trial_id"], "w1", a["lease_epoch"], 0.3)
        assert await db.reap_expired_leases(pool) == []          # live leases are not reaped

        await asyncio.sleep(0.6)                                   # w1 stalls past its lease
        reaped = await db.reap_expired_leases(pool)
        assert [r["trial_id"] for r in reaped] == [a["trial_id"]]  # w3's lease is untouched
        t = await trial(pool, a["trial_id"])
        assert (t["state"], t["worker_id"], t["lease_expires_at"]) == ("queued", None, None)
        assert t["last_error"] == "lease expired: worker w1, epoch 1, attempt 1/5"
        [ev] = await events(pool, a["trial_id"], "requeued")
        assert ev["worker_id"] == "w1" and ev["payload"]["epoch"] == 1

        b = await db.claim_trial(pool, "w2", lease_seconds=LEASE)
        assert b["trial_id"] == a["trial_id"]
        assert (b["lease_epoch"], b["attempts"]) == (2, 2)
        assert not await db.heartbeat_trial(pool, a["trial_id"], "w1", a["lease_epoch"])
        assert not await db.release_trial(pool, a["trial_id"], "w1", a["lease_epoch"], "zombie")
        assert await db.heartbeat_trial(pool, b["trial_id"], "w2", b["lease_epoch"])
        assert (await trial(pool, keep["trial_id"]))["state"] == "running"
        await assert_safe(pool, "L")
    run(pg, body)


def test_fencing_rejects_old_epoch_of_same_worker_id(pg):
    """w1 stalls, its trial is reaped, w1 restarts under the same id and re-claims
    the same trial. worker_id alone cannot tell the two incarnations apart; the
    epoch can, so the old incarnation can neither extend nor release the new lease."""
    async def body(pool):
        await db.create_experiment(pool, "F", "x", {}, trials("F", 1))
        old = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        tid = old["trial_id"]
        await expire(pool, tid)
        await db.reap_expired_leases(pool)
        new = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        assert (new["trial_id"], new["lease_epoch"]) == (tid, old["lease_epoch"] + 1)

        # The v1 fence (worker_id + state) matches the old incarnation's heartbeat.
        assert await pool.fetchval("SELECT count(*) FROM trials WHERE trial_id=$1 "
                                   "AND worker_id='w1' AND state='running'", tid) == 1
        before = await trial(pool, tid)
        assert not await db.heartbeat_trial(pool, tid, "w1", old["lease_epoch"], LEASE * 10)
        assert not await db.release_trial(pool, tid, "w1", old["lease_epoch"], "stale release")
        after = await trial(pool, tid)
        assert after["lease_expires_at"] == before["lease_expires_at"]
        assert (after["state"], after["lease_epoch"]) == ("running", 2)
        assert after["last_error"] == before["last_error"]
        assert await events(pool, tid, "released") == []

        assert await db.heartbeat_trial(pool, tid, "w1", new["lease_epoch"])
        assert await db.release_trial(pool, tid, "w1", new["lease_epoch"], "real error")
        t = await trial(pool, tid)
        assert (t["state"], t["last_error"]) == ("queued", "real error")
        await assert_safe(pool, "F")
    run(pg, body)


# ---------------------------------------------------------------- result ledger

def test_zombie_submission_is_suppressed_and_recorded(pg):
    async def body(pool):
        await db.create_experiment(pool, "Z", "x", {}, trials("Z", 1))
        zombie = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        await expire(pool, zombie["trial_id"])
        await db.reap_expired_leases(pool)
        live = await db.claim_trial(pool, "w2", lease_seconds=LEASE)

        assert await submit(pool, live, "w2") is True
        assert await submit(pool, live, "w2") is False      # same-epoch retry: plain duplicate
        assert await submit(pool, zombie, "w1") is False    # zombie: suppressed AND stale

        r = await pool.fetchrow("SELECT * FROM results WHERE trial_id=$1", live["trial_id"])
        assert (r["worker_id"], r["lease_epoch"], r["attempt"], r["stale_submission"]) == \
               ("w2", 2, 2, False)
        assert len(await events(pool, live["trial_id"], "duplicate_suppressed")) == 2
        [st] = await events(pool, live["trial_id"], "stale_submission")
        assert st["worker_id"] == "w1"
        assert {k: st["payload"][k] for k in ("accepted", "epoch", "current_epoch")} == \
               {"accepted": False, "epoch": 1, "current_epoch": 2}
        inv = await verify_invariants(pool, "Z")
        assert inv["ok"] and inv["suppressed_events"] == 2 and inv["stale_submission_events"] == 1
    run(pg, body)


def test_zombie_that_finishes_first_wins_and_is_flagged(pg):
    """First writer wins even from a dead lease (its execution did finish); the row
    is flagged stale, and the live lease holder is told its lease is gone."""
    async def body(pool):
        await db.create_experiment(pool, "W", "x", {}, trials("W", 1))
        await db.create_experiment(pool, "W1", "x", {}, trials("W1", 1), max_attempts=1)
        z1 = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        z2 = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        assert (z1["experiment_id"], z2["experiment_id"]) == ("W", "W1")
        for z in (z1, z2):
            await expire(pool, z["trial_id"])
        await db.reap_expired_leases(pool)                  # W -> queued, W1 -> failed
        live = await db.claim_trial(pool, "w2", lease_seconds=LEASE)
        assert live["trial_id"] == z1["trial_id"]

        # superseded lease (newer epoch exists) wins the race
        assert await submit(pool, z1, "w1") is True
        r = await pool.fetchrow("SELECT * FROM results WHERE trial_id=$1", z1["trial_id"])
        assert (r["worker_id"], r["lease_epoch"], r["stale_submission"]) == ("w1", 1, True)
        assert not await db.heartbeat_trial(pool, live["trial_id"], "w2", live["lease_epoch"])
        assert await submit(pool, live, "w2") is False      # current epoch: duplicate, not stale

        # revoked lease, never re-granted (here: trial had been given up as failed)
        assert await submit(pool, z2, "w1") is True
        t = await trial(pool, z2["trial_id"])
        assert t["state"] == "completed"
        assert await db.claim_trial(pool, "w3", lease_seconds=LEASE) is None

        stale = await pool.fetch("SELECT payload FROM events WHERE kind='stale_submission' "
                                 "ORDER BY event_id")
        assert [(s["payload"]["accepted"], s["payload"]["trial_state"]) for s in stale] == \
               [(True, "running"), (True, "failed")]
        for e in ("W", "W1"):
            inv = await verify_invariants(pool, e)
            assert inv["ok"] and inv["stale_results_accepted"] == 1 and inv["failed"] == 0
    run(pg, body)


def test_concurrent_submissions_have_exactly_one_winner(pg):
    async def body(*pools):
        await db.create_experiment(pools[0], "C", "x", {}, trials("C", 1))
        row = await db.claim_trial(pools[0], "w0", lease_seconds=LEASE)
        wins = await asyncio.gather(*(submit(pools[i % len(pools)], row, f"w{i % 3}")
                                      for i in range(30)))
        assert sum(wins) == 1
        assert await pools[0].fetchval("SELECT count(*) FROM results") == 1
        inv = await verify_invariants(pools[0], "C")
        assert inv["ok"] and inv["accepted_events"] == 1 and inv["suppressed_events"] == 29
    run(pg, body, pools=3)


def test_exhaustion_marks_failed_with_last_error(pg):
    async def body(pool):
        await db.create_experiment(pool, "X", "x", {}, trials("X", 1), max_attempts=2)
        await db.create_experiment(pool, "Y", "x", {}, trials("Y", 1), max_attempts=1)
        assert await pool.fetchval("SELECT max_attempts FROM experiments WHERE experiment_id='X'") == 2

        # via release (execution error)
        a = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        b = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        x = a if a["experiment_id"] == "X" else b
        y = b if x is a else a
        assert await db.release_trial(pool, x["trial_id"], "w1", x["lease_epoch"], "docker: boom 1")
        assert (await trial(pool, x["trial_id"]))["state"] == "queued"
        x2 = await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        assert (x2["trial_id"], x2["attempts"]) == (x["trial_id"], 2)
        assert await db.release_trial(pool, x["trial_id"], "w1", x2["lease_epoch"], "docker: boom 2")
        t = await trial(pool, x["trial_id"])
        assert (t["state"], t["last_error"]) == ("failed", "docker: boom 2")
        assert t["finished_at"] is not None

        # via lease expiry (crashed worker)
        await expire(pool, y["trial_id"])
        [r] = await db.reap_expired_leases(pool)
        assert r["state"] == "failed"
        t = await trial(pool, y["trial_id"])
        assert t["last_error"] == "lease expired: worker w1, epoch 1, attempt 1/1"
        assert len(await events(pool, y["trial_id"], "exhausted")) == 1

        assert await db.claim_trial(pool, "w1", lease_seconds=LEASE) is None  # failed is terminal
        for e in ("X", "Y"):
            inv = await verify_invariants(pool, e)
            assert inv["ok"] and inv["failed"] == 1 and inv["lost_results"] == 0
        rep = await compute_report(pool, "X")
        assert rep["fault_tolerance"]["failure_reasons"] == {"docker: boom 2": 1}
    run(pg, body)


def test_create_experiment_defaults(pg):
    async def body(pool):
        await db.create_experiment(pool, "D", "x", {}, trials("D", 2))
        e = await pool.fetchrow("SELECT priority, max_attempts FROM experiments")
        assert (e["priority"], e["max_attempts"]) == (0, CFG.max_attempts)
        rows = await pool.fetch("SELECT priority, max_attempts, lease_epoch FROM trials")
        assert {tuple(r) for r in rows} == {(0, CFG.max_attempts, 0)}
    run(pg, body)


def test_claims_follow_insertion_order_within_an_experiment(pg):
    """Clients rely on insertion order (the Trust Diff interleaves base/head
    arms); a batch shares one transaction, so created_at must not be now()."""
    async def body(pool):
        ids = [f"I/{tag}{i}" for i in range(4) for tag in ("z", "a")]   # z0 a0 z1 a1 ...
        await db.create_experiment(pool, "I", "x", {}, [
            {"trial_id": t, "task_id": "t", "replicate": 0, "spec": {}} for t in ids])
        got = [(await db.claim_trial(pool, "w1", lease_seconds=LEASE))["trial_id"]
               for _ in ids]
        assert got == ids
    run(pg, body)


# ---------------------------------------------------------------- fair scheduling

def test_newcomer_is_not_starved_by_a_large_experiment(pg):
    """The bug v1 had: ORDER BY created_at made a small experiment wait behind
    every queued trial of a big one created earlier."""
    async def body(pool):
        await db.create_experiment(pool, "big", "x", {}, trials("big", 200))
        for _ in range(3):
            await db.claim_trial(pool, "w1", lease_seconds=LEASE)
        await db.create_experiment(pool, "small", "x", {}, trials("small", 1))
        v1_position = await pool.fetchval(
            """SELECT pos FROM (SELECT trial_id, row_number() OVER (ORDER BY created_at) AS pos
                                FROM trials WHERE state='queued') q WHERE trial_id='small/0000'""")
        assert v1_position == 198      # where the v1 FIFO claim would have served it

        nxt = [(await db.claim_trial(pool, "w1", lease_seconds=LEASE))["trial_id"]
               for _ in range(3)]
        assert "small/0000" in nxt[:2]
    run(pg, body)


def test_round_robin_across_experiments(pg):
    async def body(pool):
        for e in "ABC":
            await db.create_experiment(pool, e, "x", {}, trials(e, 50))
        got = [(await db.claim_trial(pool, "w1", lease_seconds=LEASE))["experiment_id"]
               for _ in range(30)]
        assert got[:6] == ["A", "B", "C", "A", "B", "C"]
        assert Counter(got) == {"A": 10, "B": 10, "C": 10}
    run(pg, body)


def test_slow_experiment_cannot_monopolize_the_fleet(pg):
    """4 worker slots; experiment `slow` never finishes a trial during the test,
    `fast` finishes instantly. Fair share by *running* trials hands every freed
    slot to `fast`; FIFO, or round-robin on claim count alone, would end with all
    4 slots pinned on `slow` and `fast` stuck."""
    async def body(pool):
        await db.create_experiment(pool, "slow", "x", {}, trials("slow", 200))
        await db.create_experiment(pool, "fast", "x", {}, trials("fast", 40))
        busy = fast_done = 0
        slots_on_slow_when_fast_finished = None
        while busy < 4:                       # stop once every slot is pinned
            row = await db.claim_trial(pool, "w", lease_seconds=LEASE)
            if row is None:
                break
            if row["experiment_id"] == "slow":
                busy += 1
            else:
                assert await submit(pool, row, "w")
                fast_done += 1
                if fast_done == 40:
                    slots_on_slow_when_fast_finished = busy
        assert fast_done == 40
        assert slots_on_slow_when_fast_finished == 1
    run(pg, body)


def test_priority_is_strict(pg):
    async def body(pool):
        await db.create_experiment(pool, "low", "x", {}, trials("low", 30))
        await db.create_experiment(pool, "high", "x", {}, trials("high", 10), priority=5)
        await db.create_experiment(pool, "mid", "x", {}, trials("mid", 10), priority=1)
        got = [(await db.claim_trial(pool, "w1", lease_seconds=LEASE))["experiment_id"]
               for _ in range(50)]
        assert got == ["high"] * 10 + ["mid"] * 10 + ["low"] * 30
        assert await db.claim_trial(pool, "w1", lease_seconds=LEASE) is None
        assert await pool.fetchval("SELECT count(DISTINCT priority) FROM trials") == 3
    run(pg, body)


def test_fair_share_under_concurrent_claimers(pg):
    """With 12 concurrent claimers the split stays close to even (v1 FIFO: 60/0/0)."""
    async def body(*pools):
        for e in "ABC":
            await db.create_experiment(pools[0], e, "x", {}, trials(e, 100))
        got = []

        async def claimer(i):
            for _ in range(5):
                row = await db.claim_trial(pools[i % len(pools)], f"w{i}", lease_seconds=LEASE)
                got.append(row["experiment_id"])

        await asyncio.gather(*(claimer(i) for i in range(12)))
        counts = Counter(got)
        assert sum(counts.values()) == 60
        assert min(counts[e] for e in "ABC") >= 15, counts
        await assert_safe(pools[0], "A", "B", "C")
    run(pg, body, pools=3)


def test_claim_path_is_index_driven(pg):
    """With 20k finished experiments / 40k finished trials and 3 active
    experiments of 500 queued trials, neither half of the claim scans trials or
    experiments, and the per-experiment pick reads the index in order (a Sort
    there would mean reading the experiment's whole queue on every claim)."""
    async def body(pool):
        await pool.execute(
            """INSERT INTO experiments (experiment_id, name, created_at, last_claimed_at)
               SELECT 'old' || g, 'old', now() - interval '1 day', now() - interval '1 day'
               FROM generate_series(1, 20000) g""")
        await pool.execute(
            """INSERT INTO trials (trial_id, experiment_id, task_id, state, attempts, lease_epoch)
               SELECT 'old' || g || '/' || r, 'old' || g, 't', 'completed', 1, 1
               FROM generate_series(1, 20000) g, generate_series(1, 2) r""")
        for e in "ABC":
            await db.create_experiment(pool, e, "x", {}, trials(e, 500))
        for _ in range(10):
            await db.claim_trial(pool, "w", lease_seconds=LEASE)
        await pool.execute("ANALYZE")

        plans = {"candidates": await pool.fetch("EXPLAIN " + db.CANDIDATES_SQL)}
        for name, sql, args in (("fanout", db.CLAIM_SQL_FANOUT, ["w", 5.0, "B", 0, None]),
                                ("shared", db.CLAIM_SQL_SHARED, ["w", 5.0, "B", 0])):
            plans[name] = await pool.fetch("EXPLAIN " + sql, *args)
        for name, rows in plans.items():
            plan = "\n".join(r[0] for r in rows)
            assert "Seq Scan" not in plan, f"{name}:\n{plan}"
            if name != "candidates":       # (sorting the few candidates is fine)
                assert "Sort" not in plan, f"{name}:\n{plan}"
        assert "idx_trials_fair" in "\n".join(r[0] for r in plans["candidates"])
        assert "idx_trials_fair" in "\n".join(r[0] for r in plans["fanout"])
    run(pg, body)


# ---------------------------------------------------------------- invariant checker

def test_verify_invariants_detects_each_violation(pg):
    """The checker must not be vacuous: corrupt the history by hand, one way per
    check, and confirm each is caught."""
    async def body(pool):
        await db.create_experiment(pool, "V", "x", {}, trials("V", 5))
        rows = [await db.claim_trial(pool, "w1", lease_seconds=LEASE) for _ in range(3)]
        assert await submit(pool, rows[0], "w1")
        clean = await verify_invariants(pool, "V")
        assert clean["violations"] == {} and not clean["ok"]   # safe, but unfinished
        assert clean["lost_results"] == 4

        # a result written behind the ledger's back (no claim, no event, trial queued)
        await pool.execute("""INSERT INTO results (trial_id, experiment_id, task_id, outcome,
                              worker_id, attempt, lease_epoch) VALUES
                              ('V/0004','V','t','pass','ghost',1,9)""")
        # completed without a result
        await pool.execute("UPDATE trials SET state='completed' WHERE trial_id=$1",
                           rows[1]["trial_id"])
        # a second claim of the same (trial, epoch), and an epoch that skipped ahead
        await pool.execute("""INSERT INTO events (experiment_id, trial_id, worker_id, kind, payload)
                              VALUES ('V', $1, 'w9', 'claimed', '{"epoch": 1}')""",
                           rows[2]["trial_id"])
        await pool.execute("UPDATE trials SET lease_epoch=7 WHERE trial_id=$1", rows[2]["trial_id"])

        v = (await verify_invariants(pool, "V"))["violations"]
        assert v == {"completed_without_result": 1, "result_on_unfinished_trial": 1,
                     "results_without_accepted_event": 1, "accepted_events_mismatch": 1,
                     "results_without_claim": 1, "double_claims": 1, "epoch_mismatch": 1}, v
    run(pg, body)
