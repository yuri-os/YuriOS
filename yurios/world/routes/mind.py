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
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from yurios.mind.workspace import DeskFull, OutsideTheDesk, Workspace

router = APIRouter()


def _mind(request: Request):
    mind = request.app.state.rt.mind
    if mind is None:
        raise HTTPException(503, "the mind isn't running (MIND_ENABLED, or a test brain)")
    return mind


def _workspace(request: Request) -> Workspace:
    """The desk only, never the broad Vault debug surface."""
    rt = request.app.state.rt
    if not rt.cfg.workspace_enabled:
        raise HTTPException(409, "the workspace is disabled (WORKSPACE_ENABLED)")
    mind = getattr(rt, "mind", None)
    return mind.workspace if mind is not None and mind.workspace is not None else Workspace(
        rt.cfg.vault_dir / "workspace")


def _research_file(request: Request, name: str) -> Path:
    """One shelf source by basename; research provenance is intentionally read-only."""
    if not name or Path(name).name != name or name.startswith("."):
        raise HTTPException(400, "research document names cannot contain a path")
    root = (request.app.state.rt.cfg.vault_dir / "knowledge" / "reference").resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "that research document is not on the shelf")
    return path


@router.get("/api/mind")
async def mind_state(request: Request) -> dict:
    """Activity state, cadence, budget, goals, the shelf, pending self-edits."""
    return _mind(request).snapshot()


@router.get("/api/mind/workspace")
async def workspace_list(request: Request) -> dict:
    """The file index for the user's editable desk."""
    return {"files": [entry.as_dict() for entry in _workspace(request).list()]}


@router.get("/api/mind/workspace/file")
async def workspace_file(request: Request, path: str = "") -> dict:
    """Read one editable desk document through its sandbox."""
    try:
        return {"path": path, "text": _workspace(request).read(path)}
    except FileNotFoundError:
        raise HTTPException(404, "that workspace file does not exist") from None
    except OutsideTheDesk as e:
        raise HTTPException(400, str(e)) from None


@router.put("/api/mind/workspace/file")
async def workspace_write(request: Request) -> dict:
    """Replace one desk document, then tell every connected surface it changed."""
    body = await request.json()
    path = str(body.get("path") or "")
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(422, "text must be a string")
    try:
        entry = _workspace(request).write(path, text)
    except (OutsideTheDesk, DeskFull) as e:
        raise HTTPException(400, str(e)) from None
    rt = request.app.state.rt
    if getattr(rt, "mind", None) is not None:
        rt.mind.workspace_written(entry.path)
    rt.hub.publish("workspace", {"action": "write", **entry.as_dict()})
    return {"file": entry.as_dict()}


@router.get("/api/mind/research")
async def research_list(request: Request) -> dict:
    """Durable source documents on the research shelf, newest first."""
    root = request.app.state.rt.cfg.vault_dir / "knowledge" / "reference"
    if not root.is_dir():
        return {"files": []}
    files = [path for path in root.iterdir() if path.is_file() and not path.name.startswith(".")]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return {"files": [{"name": path.name, "bytes": path.stat().st_size,
                        "mtime": path.stat().st_mtime} for path in files]}


@router.get("/api/mind/research/file")
async def research_file(request: Request, name: str = "") -> dict:
    """Read an original research source without making its provenance editable."""
    path = _research_file(request, name)
    return {"name": path.name, "text": path.read_text(encoding="utf-8", errors="replace")}


@router.get("/api/mind/reading")
async def reading(request: Request) -> dict:
    """What her reading is doing right now, and what it is going to cost.

    The one thing in here she does entirely on her own initiative and entirely
    out of sight: a `research` tool call returns in milliseconds and then spends
    the next half hour of the machine on a document nobody has seen. This is
    that, on a page — the runs, the page being read this second with its
    passage count, the model calls it will take, and everything parked waiting
    on you (SPEC §24.3, §7.7).

    Tolerant of a mindless runtime and of a build with no search backend, since
    a panel that 503s is a panel that tells you nothing about either.
    """
    rt = request.app.state.rt
    research = getattr(rt, "research", None)
    mind = getattr(rt, "mind", None)
    store = mind.knowledge if mind is not None else None
    return {
        "search": getattr(rt, "research_status", "off"),
        "mind": mind is not None,
        "runs": research.runs() if research is not None else [],
        "reading": store.progress() if store is not None else None,
        "held": store.holds() if store is not None else [],
    }


@router.post("/api/mind/reading/stop")
async def reading_stop(request: Request) -> dict:
    """Stop a run (`{"run": "<id>"}`) or just the read in flight (`{}`).

    Nothing is thrown away: passages already read stay in the index, the
    document stays on the shelf, and both are parked until you resume them.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — an empty body means "whatever she's reading"
        pass
    rt = request.app.state.rt
    research = getattr(rt, "research", None)
    mind = getattr(rt, "mind", None)
    run_id = str(body.get("run") or "")
    if run_id:
        if research is None or not research.stop(run_id):
            raise HTTPException(404, f"no run {run_id} still going")
        return {"stopped": True, "run": run_id}
    if mind is None or not mind.knowledge.stop():
        raise HTTPException(409, "she isn't reading anything right now")
    return {"stopped": True}


@router.post("/api/mind/reading/resume")
async def reading_resume(request: Request) -> dict:
    """Let a parked document be read again. Body: {"doc": "<name>"}.

    It goes back to being pending work, and the loop picks it up on a tick like
    any other doc on the shelf — carrying on from the passage it stopped at.
    """
    body = await request.json()
    doc = str(body.get("doc") or "")
    mind = _mind(request)
    if not mind.knowledge.resume(doc):
        raise HTTPException(404, f"{doc or 'that'} isn't being held")
    return {"resumed": True, "doc": doc}


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
