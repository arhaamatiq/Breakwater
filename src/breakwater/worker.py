"""Worker process: claims trials from Postgres, executes them in Docker,
submits results through the exactly-once ledger.

Run: python -m breakwater.worker --id w1 [--concurrency 3]

Crash-safety model:
  - Every claim carries a lease and a fencing epoch; a heartbeat task extends
    the lease while executing, presenting that epoch.
  - If this process dies (SIGKILL), heartbeats stop, the lease expires, the
    reaper requeues the trial, another worker re-executes it.
  - If this process stalls (SIGSTOP / GC pause / partition) and later resumes,
    it may finish an execution that was already re-run elsewhere. Submission
    goes through INSERT ... ON CONFLICT DO NOTHING: the ledger arbitrates, the
    duplicate is suppressed and counted, never double-counted.
  - If it restarts under the same --id and re-claims a trial its old incarnation
    held, the new claim has a newer epoch, so nothing the old incarnation still
    does (heartbeat, release) can touch the new lease.
"""
import argparse
import asyncio
import signal
import sys
import time

from . import db
from .config import CFG
from .runner import run_trial, TrialExecutionError


def log(worker_id: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {worker_id}: {msg}", flush=True)


async def trial_heartbeat_loop(pool, trial_id: str, worker_id: str, epoch: int,
                               lost: asyncio.Event):
    while True:
        await asyncio.sleep(CFG.heartbeat_seconds)
        try:
            ok = await db.heartbeat_trial(pool, trial_id, worker_id, epoch)
        except Exception as e:
            log(worker_id, f"heartbeat error on {trial_id}: {e}")
            continue
        if not ok:
            # Lease was reaped (we stalled or lost connectivity). We finish the
            # in-flight execution anyway; the result ledger arbitrates and any
            # duplicate is suppressed. Aborting here would just be an optimization.
            lost.set()
            log(worker_id, f"lease LOST on {trial_id} epoch {epoch} "
                           f"(will rely on ledger arbitration)")
            return


async def execute_one(pool, worker_id: str, trial) -> None:
    trial = dict(trial)
    trial_id, epoch = trial["trial_id"], trial["lease_epoch"]
    lost = asyncio.Event()
    hb = asyncio.create_task(trial_heartbeat_loop(pool, trial_id, worker_id, epoch, lost))
    try:
        log(worker_id, f"claimed {trial_id} (attempt {trial['attempts']}, epoch {epoch})")
        result = await asyncio.to_thread(run_trial, trial)
        won = await db.submit_result(
            pool, trial_id=trial_id, experiment_id=trial["experiment_id"],
            task_id=trial["task_id"], outcome=result["outcome"], detail=result["detail"],
            exec_ms=result["exec_ms"], worker_id=worker_id, attempt=trial["attempts"],
            lease_epoch=epoch, artifacts=result["artifacts"])
        log(worker_id, f"{'result accepted' if won else 'DUPLICATE suppressed'} "
                       f"for {trial_id}: {result['outcome']}")
    except Exception as e:
        # Anything, not just TrialExecutionError: an unexpected exception (bad
        # task spec, artifact store down) used to escape and silently kill this
        # claim loop. Hand the trial back; max_attempts bounds the retries and
        # last_error keeps the reason.
        kind = "execution error" if isinstance(e, TrialExecutionError) else "unexpected error"
        log(worker_id, f"{kind} on {trial_id}: {e!r}")
        try:
            await db.release_trial(pool, trial_id, worker_id, epoch,
                                   reason=f"{kind}: {e}"[:300])
        except Exception as e2:  # lease expiry + reaper is the backstop
            log(worker_id, f"release failed on {trial_id}: {e2!r}")
    finally:
        hb.cancel()


async def claim_loop(pool, worker_id: str, stop: asyncio.Event):
    while not stop.is_set():
        try:
            trial = await db.claim_trial(pool, worker_id)
        except Exception as e:
            log(worker_id, f"claim error: {e}")
            await asyncio.sleep(1.0)
            continue
        if trial is None:
            await asyncio.sleep(0.4)
            continue
        await execute_one(pool, worker_id, trial)


async def worker_heartbeat(pool, worker_id: str, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await db.heartbeat_worker(pool, worker_id)
        except Exception:
            pass
        await asyncio.sleep(CFG.heartbeat_seconds)


async def main(worker_id: str, concurrency: int):
    pool = await db.make_pool(max_size=concurrency + 2)
    await db.register_worker(pool, worker_id)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)

    log(worker_id, f"started (concurrency={concurrency}, lease={CFG.lease_seconds}s, "
                   f"heartbeat={CFG.heartbeat_seconds}s)")
    tasks = [asyncio.create_task(worker_heartbeat(pool, worker_id, stop))]
    tasks += [asyncio.create_task(claim_loop(pool, worker_id, stop)) for _ in range(concurrency)]
    await stop.wait()
    log(worker_id, "stopping gracefully")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--concurrency", type=int, default=CFG.worker_concurrency)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.id, args.concurrency))
    except KeyboardInterrupt:
        sys.exit(0)
