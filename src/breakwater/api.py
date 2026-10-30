"""FastAPI control plane: experiment creation, status, reports, provenance.
Also hosts the reaper (single instance here; production would elect a leader)."""
import asyncio
import contextlib
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db
from .config import CFG
from .report import compute_report
from .benchmarks.suite import TASKS


class ExperimentCreate(BaseModel):
    name: str
    replicates: int = 1
    solution: str = "agent"          # agent | oracle | noop
    tasks: list[str] | None = None   # default: all benchmark tasks
    experiment_id: str | None = None
    priority: int = 0                # higher is claimed first (strict)
    max_attempts: int | None = None  # default: BW_MAX_ATTEMPTS


async def reaper_loop(app):
    while True:
        try:
            reaped = await db.reap_expired_leases(app.state.pool)
            for r in reaped:
                print(f"[reaper] {r['trial_id']}: lease expired -> {r['state']} "
                      f"(attempt {r['attempts']})", flush=True)
            dead = await db.mark_dead_workers(app.state.pool, CFG.worker_dead_after)
            for w in dead:
                print(f"[reaper] worker {w['worker_id']} marked DEAD", flush=True)
        except Exception as e:
            print(f"[reaper] error: {e}", flush=True)
        await asyncio.sleep(CFG.reaper_interval)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await db.make_pool()
    task = asyncio.create_task(reaper_loop(app))
    yield
    task.cancel()
    await app.state.pool.close()


app = FastAPI(title="Breakwater control plane", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    await app.state.pool.fetchval("SELECT 1")
    return {"ok": True}


@app.post("/experiments")
async def create_experiment(req: ExperimentCreate):
    task_ids = req.tasks or list(TASKS.keys())
    unknown = [t for t in task_ids if t not in TASKS]
    if unknown:
        raise HTTPException(400, f"unknown tasks: {unknown}")
    if req.solution not in ("agent", "oracle", "noop"):
        raise HTTPException(400, "solution must be agent|oracle|noop")
    if req.max_attempts is not None and req.max_attempts < 1:
        raise HTTPException(400, "max_attempts must be >= 1")

    exp_id = req.experiment_id or f"{req.name}-{uuid.uuid4().hex[:8]}"
    trials = [
        {"trial_id": f"{exp_id}/{task_id}/r{i}",   # deterministic trial identity
         "task_id": task_id, "replicate": i,
         "spec": {"solution": req.solution}}
        for task_id in task_ids
        for i in range(req.replicates)
    ]
    try:
        n = await db.create_experiment(app.state.pool, exp_id, req.name,
                                       {"solution": req.solution, "replicates": req.replicates},
                                       trials, priority=req.priority,
                                       max_attempts=req.max_attempts)
    except Exception as e:
        raise HTTPException(409, f"could not create experiment: {e}")
    return {"experiment_id": exp_id, "n_trials": n}


@app.get("/experiments/{exp_id}")
async def experiment_status(exp_id: str):
    pool = app.state.pool
    exists = await pool.fetchval("SELECT 1 FROM experiments WHERE experiment_id=$1", exp_id)
    if not exists:
        raise HTTPException(404, "no such experiment")
    states = {r["state"]: r["n"] for r in await pool.fetch(
        "SELECT state, count(*) AS n FROM trials WHERE experiment_id=$1 GROUP BY state", exp_id)}
    n_results = await pool.fetchval(
        "SELECT count(*) FROM results WHERE experiment_id=$1", exp_id)
    total = sum(states.values())
    return {"experiment_id": exp_id, "trials": total, "states": states,
            "results": n_results,
            "done": states.get("queued", 0) + states.get("running", 0) == 0}


@app.get("/experiments/{exp_id}/report")
async def experiment_report(exp_id: str):
    exists = await app.state.pool.fetchval(
        "SELECT 1 FROM experiments WHERE experiment_id=$1", exp_id)
    if not exists:
        raise HTTPException(404, "no such experiment")
    return await compute_report(app.state.pool, exp_id)


@app.get("/experiments/{exp_id}/events")
async def experiment_events(exp_id: str, after_id: int = 0, limit: int = 500):
    rows = await app.state.pool.fetch(
        """SELECT event_id, trial_id, worker_id, kind, payload, at FROM events
           WHERE experiment_id=$1 AND event_id > $2 ORDER BY event_id LIMIT $3""",
        exp_id, after_id, limit)
    return [{**dict(r), "at": r["at"].isoformat()} for r in rows]


@app.get("/experiments/{exp_id}/results")
async def experiment_results(exp_id: str):
    rows = await app.state.pool.fetch(
        """SELECT trial_id, task_id, outcome, attempt, worker_id, exec_ms
           FROM results WHERE experiment_id=$1 ORDER BY trial_id""", exp_id)
    return [dict(r) for r in rows]


@app.get("/experiments/{exp_id}/stream")
async def experiment_stream(exp_id: str, after_id: int = 0):
    """SSE stream of provenance events (poll-backed, 0.5s)."""
    from fastapi.responses import StreamingResponse
    import json as _json

    async def gen():
        last = after_id
        while True:
            rows = await app.state.pool.fetch(
                """SELECT event_id, trial_id, worker_id, kind, payload, at FROM events
                   WHERE experiment_id=$1 AND event_id > $2 ORDER BY event_id LIMIT 200""",
                exp_id, last)
            for r in rows:
                last = r["event_id"]
                data = _json.dumps({**dict(r), "at": r["at"].isoformat()})
                yield f"id: {r['event_id']}\nevent: {r['kind']}\ndata: {data}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/workers")
async def workers():
    rows = await app.state.pool.fetch(
        "SELECT worker_id, state, started_at, last_heartbeat FROM workers ORDER BY worker_id")
    return [{**dict(r), "started_at": r["started_at"].isoformat(),
             "last_heartbeat": r["last_heartbeat"].isoformat()} for r in rows]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG.api_host, port=CFG.api_port, log_level="warning")
