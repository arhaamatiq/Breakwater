"""Randomized fault injection against the real queue, judged by the same SQL
invariants the chaos demo reports (breakwater.report.verify_invariants).

16 simulated worker slots (asyncio tasks over 4 connection pools) run the real
claim / heartbeat / release / submit protocol against Postgres with 250 ms
leases while a reaper runs concurrently, and misbehave at random per claim:

  ok      execute briefly, heartbeat, submit
  slow    execute for 2-3 leases, heartbeating: the lease must survive
  stall   stop for 2-4 leases without heartbeating (GC pause, SIGSTOP,
          partition), then heartbeat (normally fenced) and submit late
  crash   abandon the trial, no heartbeat or submit (SIGKILL); the slot goes on
          under the same worker id, i.e. a restart
  double  submit twice (client retry after a lost ack)
  late    hand the submission to a detached task that delivers it 0.5-3 leases
          later while the slot moves on (slow network path, zombie copy)
  error   execution error -> release (retry, or `failed` after max_attempts)

4 worker ids are shared by 16 slots, so trials are regularly re-claimed under
the *same* worker id with a newer epoch while the old holder is still stalled,
the case a worker_id-only fence gets wrong.

Seeds fix each slot's fault sequence (Random(f"{seed}:{slot}")); they do not
fix the interleaving, which asyncio + Postgres scheduling vary run to run. A
seed reproduces the fault mix, not the exact history; failures print the seed
and the invariant counts.

Asserted, per seed:
  * safety invariants hold at every sampled instant during the run (sampled
    concurrently, ~every 50 ms), not only at the end
  * at the end, for every experiment: each trial has exactly one result row or
    is `failed`; results == result_accepted events; every result traces to the
    claim (worker, epoch) that produced it; executions >= results
  * the clients' view agrees with the ledger: submit_result returned True
    exactly as many times as there are result rows, and the slots' claim count
    equals the `claimed` events
  * every fault path actually fired (stale wins and suppressions, plain
    duplicates, fenced heartbeats incl. same-worker-id re-claims, requeues,
    failed trials), so the run is not vacuously green

What this does not establish: exactly-once *execution* (trials do run more
than once, by design), behaviour under Postgres failover, or clock skew between
hosts (leases are evaluated on the database clock only).
"""
import asyncio
import random
from collections import Counter

import pytest

from breakwater import db
from breakwater.report import verify_invariants

LEASE = 0.25
FAULTS = ("ok", "slow", "stall", "crash", "double", "late", "error")
WEIGHTS = (0.51, 0.08, 0.10, 0.08, 0.08, 0.08, 0.07)
EXPERIMENTS = {  # id: (trials, priority, max_attempts)
    "A": (150, 0, 5),
    "B": (100, 0, 3),
    "C": (50, 1, 1),   # one strike: exercises `failed`, and stale wins over failed
}


async def run_chaos(dsn: str, seed: int, n_slots: int = 16, n_ids: int = 4) -> dict:
    pools = [await db.make_pool(dsn, min_size=1, max_size=6) for _ in range(4)]
    side = await db.make_pool(dsn, min_size=1, max_size=4)
    stats: Counter = Counter()
    fenced: list[tuple[str, str, int]] = []    # (trial, worker, epoch) of rejected heartbeats
    unsafe: list[dict] = []
    detached: list[asyncio.Task] = []
    stop = asyncio.Event()

    for e, (n, prio, max_att) in EXPERIMENTS.items():
        await db.create_experiment(
            side, e, e, {}, [{"trial_id": f"{e}/{i:03d}", "task_id": "t", "replicate": i,
                              "spec": {}} for i in range(n)],
            priority=prio, max_attempts=max_att)

    async def submit(pool, row, wid, delay=0.0):
        await asyncio.sleep(delay)
        won = await db.submit_result(
            pool, trial_id=row["trial_id"], experiment_id=row["experiment_id"],
            task_id=row["task_id"], outcome="pass", detail={}, exec_ms=1.0, worker_id=wid,
            attempt=row["attempts"], lease_epoch=row["lease_epoch"], artifacts=[])
        stats["submit_won" if won else "submit_lost"] += 1

    async def heartbeat(pool, row, wid):
        if not await db.heartbeat_trial(pool, row["trial_id"], wid, row["lease_epoch"], LEASE):
            fenced.append((row["trial_id"], wid, row["lease_epoch"]))
            return False
        return True

    async def slot(i):
        rng = random.Random(f"{seed}:{i}")
        pool, wid = pools[i % len(pools)], f"w{i % n_ids}"
        while not stop.is_set():
            row = await db.claim_trial(pool, wid, lease_seconds=LEASE)
            if row is None:
                await asyncio.sleep(0.01)
                continue
            stats["claims"] += 1
            fault = rng.choices(FAULTS, WEIGHTS)[0]
            stats[fault] += 1
            if fault == "crash":
                continue
            if fault == "late":
                detached.append(asyncio.create_task(
                    submit(pool, row, wid, delay=LEASE * rng.uniform(0.5, 3.0))))
                continue
            if fault == "stall":
                await asyncio.sleep(LEASE * rng.uniform(2.0, 4.0))
            elif fault == "slow":
                for _ in range(rng.randint(6, 9)):         # 2-3 leases, beating every lease/3
                    await asyncio.sleep(LEASE / 3)
                    if not await heartbeat(pool, row, wid):
                        stats["slow_lost_lease"] += 1
                        break
            else:
                await asyncio.sleep(rng.uniform(0.001, 0.02))
            await heartbeat(pool, row, wid)
            if fault == "error":
                if not await db.release_trial(pool, row["trial_id"], wid, row["lease_epoch"],
                                              "injected execution error"):
                    stats["fenced_release"] += 1
                continue
            await submit(pool, row, wid)
            if fault == "double":
                await submit(pool, row, wid)

    async def reaper():
        while not stop.is_set():
            await db.reap_expired_leases(side)
            await asyncio.sleep(0.05)

    async def sampler():
        while not stop.is_set():
            for e in EXPERIMENTS:
                inv = await verify_invariants(side, e)
                stats["safety_samples"] += 1
                if inv["violations"]:
                    unsafe.append({"experiment": e, **inv["violations"]})
            await asyncio.sleep(0.05)

    async def until_drained():
        while await side.fetchval(
                "SELECT count(*) FROM trials WHERE state IN ('queued','running')"):
            await asyncio.sleep(0.05)

    workers = [asyncio.create_task(slot(i)) for i in range(n_slots)]
    helpers = [asyncio.create_task(reaper()), asyncio.create_task(sampler())]
    try:
        await asyncio.wait_for(until_drained(), timeout=90)
    finally:
        stop.set()
        await asyncio.gather(*workers, *helpers)
        await asyncio.gather(*detached)          # zombies deliver after the run "ended"

    invariants = {e: await verify_invariants(side, e) for e in EXPERIMENTS}
    claims = {(r["trial_id"], r["worker_id"], r["epoch"]) for r in await side.fetch(
        "SELECT trial_id, worker_id, (payload->>'epoch')::bigint AS epoch "
        "FROM events WHERE kind = 'claimed'")}
    # fenced heartbeats from a worker id that had re-claimed the same trial under
    # a newer epoch: only the epoch can tell those two incarnations apart
    stats["fenced_same_id_reclaim"] = sum(
        any((t, w, later) in claims for later in range(ep + 1, ep + 6)) for t, w, ep in fenced)
    stats["fenced_heartbeat"] = len(fenced)
    events = dict(await side.fetch("SELECT kind, count(*) FROM events GROUP BY kind"))
    stale_accepted = await side.fetchval(
        "SELECT count(*) FROM results WHERE stale_submission")
    for p in (*pools, side):
        await p.close()
    return {"stats": stats, "invariants": invariants, "unsafe": unsafe, "events": events,
            "stale_accepted": stale_accepted}


@pytest.mark.parametrize("seed", [7, 1234, 20261029])
def test_fault_injection_preserves_exactly_once_accounting(pg, seed):
    out = asyncio.run(run_chaos(pg, seed))
    stats, inv, ev = out["stats"], out["invariants"], out["events"]
    ctx = f"seed={seed} stats={dict(stats)} events={ev}"

    assert out["unsafe"] == [], f"safety violated mid-run: {out['unsafe'][:3]} {ctx}"
    assert stats["safety_samples"] > 0
    for e, (n, _, _) in EXPERIMENTS.items():
        i = inv[e]
        assert i["ok"], f"{e}: {i} {ctx}"
        assert i["trials"] == n and i["results"] + i["failed"] == n, f"{e}: {i}"
        assert i["executions_started"] >= i["results"]

    n_results = sum(i["results"] for i in inv.values())
    assert stats["submit_won"] == n_results == ev["result_accepted"], ctx
    assert stats["claims"] == ev["claimed"] == sum(i["executions_started"] for i in inv.values())
    assert stats["claims"] > n_results, ctx            # at-least-once: re-executions happened

    # non-vacuity: every fault path fired and was observed where it should be
    assert out["stale_accepted"] > 0, ctx              # zombie won the race, flagged
    assert ev.get("stale_submission", 0) > out["stale_accepted"], ctx  # ...and some lost it
    assert ev.get("duplicate_suppressed", 0) >= stats["double"] > 0, ctx
    assert stats["fenced_heartbeat"] > 0 and stats["fenced_same_id_reclaim"] > 0, ctx
    assert ev.get("requeued", 0) > 0 and ev.get("released", 0) > 0, ctx
    assert sum(i["failed"] for i in inv.values()) > 0, ctx
