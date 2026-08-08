"""/api/mind — the inner-life surface (SPEC §24.3).

The journal and the dashboard are the product half of autonomy: "what did she
do while I was gone" must be a page you open, not a vibe. Everything here
reads *through* the mind's own stores — the same files she reads — so the
dashboard can never disagree with reality. The one write path is the self-edit
decision, and even that is only a signal: the loop consumes it on its next
tick, exactly like everything else that happens to her.
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _mind(request: Request):
    mind = request.app.state.rt.mind
    if mind is None:
        raise HTTPException(503, "the mind isn't running (MIND_ENABLED, or a test brain)")
    return mind


@router.get("/api/mind")
async def mind_state(request: Request) -> dict:
    """Activity state, cadence, budget, goals, the shelf, pending self-edits."""
    return _mind(request).snapshot()


@router.get("/api/mind/journal")
async def journal(request: Request, days: int = 3) -> dict:
    """The last `days` of the shared journal — her acts flagged `hers`."""
    mind = _mind(request)
    now = mind.clock.now()
    out = []
    for i in range(max(1, min(days, 30))):
        day = datetime.datetime.fromtimestamp(
            now - i * 86400).strftime("%Y-%m-%d")
        entries = mind.journal.day_entries(day)
        if entries:
            out.append({"day": day, "entries": entries})
    return {"days": out}


@router.get("/api/mind/trace")
async def trace(request: Request, n: int = 40) -> dict:
    """The tick trace tail — the why-record behind the journal."""
    return {"ticks": _mind(request).trace.tail(max(1, min(n, 200)))}


@router.get("/api/mind/dream")
async def dream_status(request: Request) -> dict:
    """The night's roster: every job, whether it's on, and what it still owes.

    Reads the same runner the tick loop uses, so the page can never show a job
    list the loop doesn't have.
    """
    mind = _mind(request)
    return {"jobs": mind.dreams.status(),
            "backlog": mind.dreams.backlog(),
            "state": mind.activity.state,
            "window": [mind.cfg.mind_dream_start_hour,
                       mind.cfg.mind_dream_end_hour],
            "enabled": bool(mind.cfg.dream_enabled and mind.cfg.utility_enabled),
            "tick_budget": mind.cfg.mind_dream_tick_tokens}


@router.post("/api/mind/dream/run")
async def dream_run(request: Request) -> dict:
    """Run DREAM now, by hand.

    Body, all optional:
      {"job": "diary",          — one job instead of the whole night
       "day": "2026-08-07",     — pin the day, instead of taking its backlog
       "dry_run": true,         — do the thinking, write nothing
       "budget": 40000}         — override the per-tick token budget

    Answers with the full report *including the prompts* — the exact system
    message, the exact input and the raw completion for every model call the
    run made. That is the whole point of the button: a dream job is a prompt
    you wrote and cannot otherwise see the output of until tomorrow morning,
    and "run it against yesterday, dry, and show me what came back" is the only
    way to iterate on one in less than a day.

    Runs inline rather than posting a signal, which is the opposite of the
    self-edit route next door and deliberately so: a decision belongs to the
    loop's next tick, but a test you are watching has to answer *you*.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — an empty body means "the whole night"
        pass
    mind = _mind(request)
    if not mind.cfg.dream_enabled:
        raise HTTPException(409, "DREAM is off for this character (DREAM_ENABLED)")
    kw = {"dry_run": bool(body.get("dry_run")),
          "token_budget": int(body.get("budget") or mind.cfg.mind_dream_tick_tokens)}
    if body.get("job"):
        kw["only"] = str(body["job"])
    if body.get("day"):
        kw["day"] = str(body["day"])
    try:
        report = await mind.dream_now(**kw)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    return report.as_dict()


@router.post("/api/mind/edits/{edit_id}")
async def decide_edit(edit_id: str, request: Request) -> dict:
    """Rule on a queued self-edit. Body: {"approve": true|false}. The decision
    is a signal; the loop applies (or rejects) it on its next tick, commits it,
    and journals what you decided — so even your rulings leave a trail."""
    body = await request.json()
    mind = _mind(request)
    if not any(p["id"] == edit_id for p in mind.selfedit.pending()):
        raise HTTPException(404, f"no pending edit {edit_id}")
    request.app.state.rt.signals.post(
        "selfedit_decision",
        {"id": edit_id, "approve": bool(body.get("approve"))}, source="user")
    return {"queued": True, "id": edit_id}
