"""What she did, and why, as one joined graph (SPEC §24.4).

`debug.py` answers one log at a time. This module answers the question none of
them can alone: *what led to this?* It reads every record about her in a time
window — ticks, the conversation, model calls, the utility model's runs, her
hands, the camera, the journal, goals, state changes, Vault commits — and joins
them into one list of events, the links between them, and the stories the
mind debug page draws (a decision, an exchange, a reach-out).

Two kinds of link, and the difference is the point of the page:

  - **Recorded.** A shared `tick_id` or `corr_id`, the signal ids SENSE wrote
    down, a goal named by the tick that served it, a reply's `turn_id` equal to
    the `messages_ref` of the call that wrote it, a commit whose message names
    its tick or turn. The files say so.
  - **Inferred.** Everything joined by *time*: a reach-out and the composing
    call just before it, a `[she]` line and the tick around its minute (a
    journal heading carries only HH:MM). Each inferred link carries the reason
    it was matched, and the page **MUST** show that it is inferred. Nearness in
    time is not proof of cause, and a page that drew the two alike would be
    making up facts she never wrote down.

It reads the rolled `.1` generation beside each log too — the history the other
views admit they cannot see — and writes nothing. Built on a worker thread by
the host route, cached on the sources' stat signature.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from yurios.app import vaultgit
from yurios.app.conversation import read_entries
from yurios.mind.journal import is_canonical_day, parse_day_entries
from yurios.mind.util import jsonl_reverse, read_json

from . import debug

Event = dict[str, Any]

PREVIEW = 480
CHAT_PREVIEW = 720
COMPLETION_PREVIEW = 900
ARG_PREVIEW = 420

#: Inbox traffic that says something. Presence chatter (`user_present` every few
#: seconds a tab is open) would bury the rest; SENSE still lists it on the tick.
SIGNAL_KEEP = frozenset({
    "wakeup", "timer", "turn_committed", "task_completion",
    "suspend_gap", "goal_decision", "selfedit_decision", "user_message",
})
#: Reach-outs are composed by these prompt kinds, a little before they land.
REACH_OUT_PROMPTS = frozenset({"greeting", "compose"})
REACH_OUT_WINDOW_S = 120
#: ACT delivers first and REFLECT traces the tick afterwards, so the tick that
#: spoke a message is looked for *after* it, never before.
SPOKE_AFTER_TICK_S = 180
JOURNAL_AFTER_TICK_S = 120
#: The logs are appended in roughly time order, not strictly: a tick is traced
#: after its own calls. A windowed read stops only once rows are this far past
#: the edge, so a straggler just inside it is not lost.
WINDOW_SLACK_S = 3600
#: The widest finite window. Past a year the page asks for everything instead,
#: which is what "all" already means.
MAX_DAYS = 366.0
#: Vault commits read per build, at most — git stops at the window's edge first.
COMMIT_LIMIT = 500
GOAL_META_KEYS = (
    "about", "source", "rationale", "evidence", "success", "first_action",
    "capability", "strategy_note", "steps", "last_step",
)

_TICK_SUBJECT = re.compile(r"^tick (t-[0-9a-f]+):")
_TURN_SUBJECT = re.compile(r"^turn ([0-9a-f]+):(\d+)$")


# --- small pieces -------------------------------------------------------------

def when(value: Any) -> float | None:
    """Epoch seconds from either convention on disk: an epoch number (tool
    audits, activity `at`) or an ISO string, naive meaning house-local
    (`mind.util.iso_of`) and aware keeping its offset (utility, context)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e12 else v       # a few older rows stamped ms
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def stable_id(prefix: str, row: dict) -> str:
    """An id for a row that has none, from its content — never its position,
    which rotation shifts, so a bookmarked record stays the same record."""
    payload = json.dumps(row, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def clip(text: Any, n: int = PREVIEW) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def clip_block(text: Any, n: int) -> str:
    """`clip`, keeping paragraph breaks — for text read as prose."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def small(obj: Any, n_keys: int) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= n_keys:
                out["…"] = f"{len(obj) - n_keys} more"
                break
            if isinstance(v, str):
                out[k] = clip(v, 280)
            elif isinstance(v, (int, float, bool)) or v is None:
                out[k] = v
            else:
                out[k] = clip(v, 200)
        return out
    return clip(obj, 280)


def spoken_when(unix: float | None) -> str:
    """House-local, the way a person says it: 'Sat 19 Sep 11:31'."""
    if unix is None:
        return "At an unknown time"
    return datetime.fromtimestamp(unix).strftime("%a %d %b %H:%M")


def last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):          # multimodal parts
                content = " ".join(p.get("text", "") for p in content
                                   if isinstance(p, dict))
            return clip(content or "", 360)
    return ""


def intention_bucket(intention: str) -> str:
    if not intention or intention == "REST":
        return "REST"
    return intention.split(":")[0][:24]


def intention_subject(intention: str) -> str:
    """'goal:finish the note' → 'finish the note'; a bare intention → ''."""
    if ":" not in (intention or ""):
        return ""
    return intention.split(":", 1)[1].strip()


# --- reading a window -----------------------------------------------------------

def window_rows(path: Path, since: float | None,
                stamp: Callable[[dict], float | None]) -> list[dict]:
    """A log's rows from `since` on, oldest first, across the live file and its
    rolled `.1` — read backwards, and stopped once rows are well past the edge,
    so a week of a year-long log costs a week.

    A row both generations hold (a rotation interrupted between copy and
    truncate) is kept once."""
    out: list[dict] = []
    seen: set[str] = set()
    for candidate in (path, path.with_name(path.name + ".1")):
        exhausted = True
        for row in jsonl_reverse(candidate):
            t = stamp(row)
            if since is not None and t is not None and t < since:
                if t < since - WINDOW_SLACK_S:
                    exhausted = False
                    break
                continue
            key = str(row.get("id") or row.get("tick_id") or row.get("call_id")
                      or json.dumps(row, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        if not exhausted:
            break
    out.reverse()
    return out


def _stamp(*fields: str) -> Callable[[dict], float | None]:
    def read(row: dict) -> float | None:
        for field in fields:
            t = when(row.get(field))
            if t is not None:
                return t
        return None
    return read


def parse_goal(goal: dict) -> dict:
    """A `GoalStore` goal, in the page's words: `title` for its text, `from`
    for its provenance, `commit` for its commitment."""
    meta = goal.get("meta") or {}
    return {
        "id": goal.get("id", ""),
        "title": (goal.get("text") or "").strip(),
        "kind": goal.get("kind") or "task",
        "priority": float(goal.get("priority") or 0),
        "state": goal.get("state") or "pending",
        "from": goal.get("provenance") or "",
        "created": goal.get("created") or "",
        "due": goal.get("due") or "",
        "commit": goal.get("commitment") or "",
        "meta": {k: (clip(meta[k], 600) if isinstance(meta[k], str) else meta[k])
                 for k in GOAL_META_KEYS
                 if k in meta and meta[k] not in ("", None, {}, [])},
    }


def journal_events(episodic: Path, since: float | None) -> list[Event]:
    """Her `[she]` lines, read with the journal's own parser. The lines written
    *together* are the conversation, which the chat lane already carries."""
    events: list[Event] = []
    if not episodic.is_dir():
        return events
    first_day = (datetime.fromtimestamp(since).strftime("%Y-%m-%d")
                 if since is not None else "")
    for path in sorted(episodic.glob("*.md")):
        day = path.stem
        if not is_canonical_day(day) or day < first_day:
            continue
        entries = parse_day_entries(path.read_text(encoding="utf-8", errors="replace"))
        minute: Counter = Counter()
        for entry in entries:
            if not entry["hers"]:
                continue
            hhmm, text = entry["time"], entry["text"].strip()
            unix = when(f"{day}T{hhmm}:00")
            if unix is None:
                continue
            # Several lines can share a minute; a second apiece keeps their order.
            offset = minute[hhmm]
            minute[hhmm] += 1
            events.append({
                "id": stable_id("j", {"day": day, "minute": hhmm, "text": text}),
                "t": unix + offset,
                "kind": "journal", "lane": "journal",
                "title": clip(text, 90), "summary": clip(text, 500),
                "ref": f"#/vault/file/memory/episodic/{day}.md",
                "detail": {"text": clip_block(text, 1600)},
            })
    return events


# --- one tick, shaped ---------------------------------------------------------

def tick_event(row: dict, goal_by_id: dict[str, dict],
               goal_by_title: dict[str, str]) -> Event | None:
    """A non-REST tick as an event, or None for REST (a heartbeat that chose
    nothing is density, not an event). Shared with the tick detail view so the
    two never describe one tick differently."""
    decided = row.get("decided") or {}
    acted = row.get("acted") or {}
    intention = decided.get("intention") or "REST"
    bucket = intention_bucket(intention)
    if bucket == "REST" or acted.get("result") == "rest":
        return None
    tid = row.get("tick_id") or stable_id("t", row)
    appraisals = [
        # the hands list rides on every tool_step why; it is kept once, in hands
        {"what": clip(a.get("what"), 140), "score": a.get("score_to_act"),
         "why": clip(str(a.get("why") or "").split("; hands:")[0], 200)}
        for a in (row.get("appraised") or [])[:8] if isinstance(a, dict)
    ]
    top = next((a for a in appraisals if a["what"] == intention), None) \
        or (appraisals[0] if appraisals else {})
    hands = decided.get("hands") or {}
    available = hands.get("available") or []
    goal_id = acted.get("goal") or ""
    if not goal_id:
        goal_id = goal_by_title.get(intention_subject(intention).lower(), "")
    return {
        "id": tid, "t": when(row.get("ts")),
        "kind": "tick", "lane": "ticks",
        "title": clip(intention, 110),
        "summary": clip(acted.get("result") or top.get("why"), 240),
        "state": row.get("activity_state") or "?",
        "tick_id": tid, "bucket": bucket,
        "goal": goal_id if goal_id in goal_by_id else "",
        "ref": f"#/ticks/detail/{tid}",
        "detail": {
            "sensed": [{"type": s.get("type"), "id": s.get("id")}
                       for s in (row.get("sensed") or []) if isinstance(s, dict)],
            "appraised": appraisals,
            "intention": intention,
            "runners_up": [clip(r, 120) for r in (decided.get("runners_up") or [])[:5]],
            "hands": {"available": available[:16],
                      "more": max(0, len(available) - 16),
                      "blocked": hands.get("blocked") or ""},
            "acted": {k: acted.get(k) for k in ("what", "result", "goal", "tool",
                                                "verdict", "state")
                      if acted.get(k) not in (None, "")},
            "interrupt": row.get("interrupt") or {},
            "why": top.get("why") or "",
            "score": top.get("score"),
        },
    }


def explain_tick(ev: Event, goal_by_id: dict[str, dict], act_threshold: float) -> str:
    """One decision, in the order the tick lived it."""
    d = ev["detail"]
    bits = [f"{spoken_when(ev['t'])}, she was {ev.get('state') or 'awake'}."]
    sensed = d["sensed"]
    if sensed:
        types = ", ".join(s.get("type") or "?" for s in sensed)
        bits.append(f"SENSE picked up {len(sensed)} signal"
                    f"{'s' if len(sensed) != 1 else ''}: {types}.")
    else:
        bits.append("SENSE was empty: nothing new on the bus since the last heartbeat.")
    if d["appraised"]:
        ranked = "; ".join(f"{clip(a['what'], 60)} ({a['score']})" for a in d["appraised"][:3])
        bits.append(f"APPRAISE scored the candidates without calling a model: {ranked}. "
                    f"Gate 1 fires at {act_threshold}.")
    runners = d["runners_up"]
    extra = f" Runners-up: {', '.join(clip(r, 50) for r in runners)}." if runners else ""
    bits.append(f"DECIDE committed to «{clip(d['intention'], 90)}».{extra}")
    if d.get("why"):
        bits.append(f"Why it won: {d['why']}.")
    goal = goal_by_id.get(ev.get("goal") or "")
    if goal:
        bits.append(f"It served the goal “{clip(goal['title'], 90)}” ({goal['state']}).")
    acted = d["acted"]
    if acted.get("what"):
        bits.append(f"ACT: {acted['what']} → {acted.get('result') or 'no result recorded'}.")
    if d["hands"].get("blocked"):
        bits.append(f"Hands were blocked: {d['hands']['blocked']}.")
    interrupt = d.get("interrupt") or {}
    if interrupt.get("outcome"):
        bits.append(f"Gate 2 (interrupt) scored {interrupt.get('score')} against "
                    f"{interrupt.get('threshold')} → {interrupt['outcome']}.")
    return " ".join(bits)


# --- the build ----------------------------------------------------------------

def build(record, *, days: float | None = 7.0, char_name: str = "Her",
          user_name: str = "you", act_threshold: float = 0.4,
          now: float | None = None) -> dict:
    """The joined graph for the last `days` (None: everything retained)."""
    now = time.time() if now is None else now
    since = None if days is None else now - days * 86400
    vault = Path(record.paths.vault)
    src = lambda name: debug.source(record, name)             # noqa: E731

    ticks = window_rows(src("ticks"), since, _stamp("ts"))
    activity = window_rows(src("activity"), since, _stamp("at", "ts"))
    signals = window_rows(src("signals"), since, _stamp("ts", "at"))
    prompts = window_rows(src("prompts"), since, _stamp("at", "ts"))
    context = window_rows(src("context"), since, _stamp("timestamp"))
    utility = window_rows(src("utility"), since, _stamp("timestamp"))
    calls = window_rows(src("calls"), since, _stamp("ts"))
    selfies = window_rows(src("generations"), since, _stamp("created_at"))
    conversation = [r for r in read_entries(vault)
                    if since is None or (when(r.get("ts")) or 0) >= since]
    goals = [parse_goal(g) for g in reversed(debug.goals(record)["items"])]
    journal = journal_events(vault / "memory" / "episodic", since)
    commits = [c for c in vaultgit.log_records(vault, limit=COMMIT_LIMIT, since=since)
               if since is None or float(c.get("at") or 0) >= since]
    activity_now = read_json(vault / "state" / "activity.json", {}) or {}
    budget = read_json(vault / "state" / "budget.json", {}) or {}
    situation_path = vault / "world" / "situation.md"
    situation = (clip(situation_path.read_text(encoding="utf-8"), 600)
                 if situation_path.is_file() else "")
    selfie_url = lambda image: (                               # noqa: E731
        f"/api/characters/{record.id}/selfies/{Path(str(image)).name}" if image else "")

    events: list[Event] = []
    rest_hours: dict[int, Counter] = defaultdict(Counter)
    rest_times: dict[int, list[float]] = defaultdict(list)
    prompt_by_tick: dict[str, list[str]] = defaultdict(list)
    tools_by_tick: dict[str, list[str]] = defaultdict(list)
    tools_by_turn: dict[tuple[str, int], list[str]] = defaultdict(list)
    by_corr: dict[str, list[str]] = defaultdict(list)
    prompt_by_turn: dict[str, str] = {}
    tally: dict[str, int] = Counter()
    counters: dict[str, Counter] = defaultdict(Counter)
    tokens = {"in": 0, "out": 0}
    used_ids: set[str] = set()

    def unique(ev_id: str) -> str:
        base, n = ev_id, 1
        while ev_id in used_ids:
            n += 1
            ev_id = f"{base}~{n}"
        used_ids.add(ev_id)
        return ev_id

    # -- activity transitions ------------------------------------------------
    activity_rows: list[dict[str, Any]] = []
    for row in activity:
        t = when(row.get("at")) or when(row.get("ts"))
        if t is None:
            continue
        activity_rows.append({"t": t, "from": row.get("from"), "to": row.get("to"),
                              "reason": row.get("reason"),
                              "cadence_s": row.get("cadence_s")})
        events.append({
            "id": unique(str(row.get("id") or stable_id("act", row))),
            "t": t, "kind": "activity", "lane": "activity",
            "title": f"{row.get('from') or '·'} → {row.get('to')}",
            "summary": f"{row.get('reason') or 'no reason given'} · "
                       f"cadence {row.get('cadence_s')}s",
            "state": row.get("to"), "ref": "#/overview",
            "detail": {"from": row.get("from"), "to": row.get("to"),
                       "reason": row.get("reason"), "cadence_s": row.get("cadence_s")},
        })
        tally["activity"] += 1
    activity_rows.sort(key=lambda r: r["t"])

    # -- goals, filed as events at the moment she took them on ----------------
    goal_by_id = {g["id"]: g for g in goals}
    goal_by_title = {g["title"].lower(): g["id"] for g in goals}
    for g in goals:
        g["t"] = when(g["created"])
        g["ticks"] = []
        if g["t"] is None or (since is not None and g["t"] < since):
            continue
        meta = g["meta"]
        events.append({
            "id": unique(g["id"]), "t": g["t"], "kind": "goal", "lane": "goals",
            "title": clip(g["title"], 110),
            "summary": clip(meta.get("rationale") or meta.get("about")
                            or f"{g['kind']} · {g['state']} · from {g['from']}", 280),
            "goal": g["id"], "state": g["state"],
            "ref": f"#/vault/file/workspace/goals/{g['id']}.md",
            "detail": {"goal": g["id"]},
        })

    # -- ticks ---------------------------------------------------------------
    seen_ticks: set[str] = set()
    for row in ticks:
        tid = row.get("tick_id") or stable_id("t", row)
        if tid in seen_ticks:
            continue
        seen_ticks.add(tid)
        t = when(row.get("ts"))
        state = row.get("activity_state") or "?"
        tally["ticks"] += 1
        counters["intentions"][intention_bucket(
            (row.get("decided") or {}).get("intention") or "REST")] += 1
        counters["states"][state] += 1
        ev = tick_event(row, goal_by_id, goal_by_title)
        if ev is None:
            tally["rest"] += 1
            if t is not None:
                hour = int(t // 3600 * 3600)
                rest_hours[hour][state] += 1
                rest_times[hour].append(t)
            continue
        if t is None:
            continue
        tally["acted"] += 1
        ev["id"] = unique(ev["id"])
        if ev["goal"]:
            goal_by_id[ev["goal"]]["ticks"].append(ev["id"])
        events.append(ev)

    rest_density = [
        {"t": hour, "n": sum(counts.values()), "state": counts.most_common(1)[0][0],
         "first_t": min(rest_times[hour]), "last_t": max(rest_times[hour])}
        for hour, counts in sorted(rest_hours.items())
    ]

    # -- signals -------------------------------------------------------------
    for row in signals:
        stype = row.get("type") or "signal"
        counters["signal_types"][stype] += 1
        if stype not in SIGNAL_KEEP:
            continue
        t = when(row.get("ts")) or when(row.get("at"))
        if t is None:
            continue
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        goal_ref = payload.get("goal") if isinstance(payload.get("goal"), str) else ""
        goal_title = goal_by_id[goal_ref]["title"] if goal_ref in goal_by_id else ""
        hours = payload.get("hours")
        hours_text = f"away {hours:.1f} h" if isinstance(hours, (int, float)) else hours
        events.append({
            "id": unique(row.get("id") or stable_id("sig", row)),
            "t": t, "kind": "signal", "lane": "signals",
            "title": stype,
            "summary": clip(payload.get("text") or goal_title or payload.get("goal")
                            or payload.get("task") or payload.get("label")
                            or hours_text or (payload or ""), 200),
            "signal_type": stype,
            "goal": goal_ref if goal_ref in goal_by_id else "",
            "ref": "#/signals",
            "detail": {"type": stype, "source": row.get("source"),
                       "payload": small(payload, 8)},
        })
        tally["signals"] += 1

    # -- model calls (bodies stay on disk; the ref opens the whole prompt) ----
    for row in prompts:
        t = when(row.get("at")) or when(row.get("ts"))
        if t is None:
            continue
        kind = row.get("kind") or "prompt"
        counters["prompt_kinds"][kind] += 1
        tokens["in"] += int(row.get("tokens_in") or 0)
        tokens["out"] += int(row.get("tokens_out") or 0)
        pid = unique(row.get("id") or stable_id("pr", row))
        tid, cid = row.get("tick_id"), row.get("corr_id")
        ref = row.get("messages_ref") or {}
        if isinstance(ref, dict) and ref.get("id"):
            prompt_by_turn[str(ref["id"])] = pid
        model = row.get("model") or ""
        events.append({
            "id": pid, "t": t, "kind": "prompt", "lane": "prompts",
            "title": f"{kind} · {clip(model.split('/')[-1], 40)}",
            "summary": clip(row.get("completion") or last_user_text(row.get("messages")),
                            COMPLETION_PREVIEW),
            "tick_id": tid, "corr_id": cid, "prompt_kind": kind,
            "ref": f"#/context/prompt/{row['id']}" if row.get("id") else "",
            "detail": {
                "kind": kind, "origin": row.get("origin"), "model": model,
                "tier": row.get("tier"), "tokens_in": row.get("tokens_in"),
                "tokens_out": row.get("tokens_out"), "n_messages": row.get("n_messages"),
                "truncated": bool(row.get("truncated")),
                "asked": last_user_text(row.get("messages")),
                "completion": clip_block(row.get("completion"), COMPLETION_PREVIEW),
            },
        })
        tally["prompts"] += 1
        if tid:
            prompt_by_tick[tid].append(pid)
        if cid:
            by_corr[cid].append(pid)

    # -- the conversation (what was drawn, folded as the log folds it) -------
    for row in conversation:
        role = row.get("role")
        if role not in ("user", "assistant"):
            continue
        t = when(row.get("ts"))
        if t is None:
            continue
        text = row.get("text") or row.get("raw") or ""
        if not text and row.get("image_url"):
            text = "[a photo]"
        proactive = bool(row.get("proactive"))
        events.append({
            "id": unique(f"chat-{row['id']}" if row.get("id") else stable_id("chat", row)),
            "t": t, "kind": "chat", "lane": "chat",
            "title": (user_name if role == "user" else char_name)
                     + (" · reach-out" if proactive else ""),
            "summary": clip(text, CHAT_PREVIEW),
            "role": role, "proactive": proactive,
            "tick_id": row.get("tick_id"), "corr_id": row.get("corr_id"),
            "turn_id": row.get("turn_id") or "",
            "ref": "",
            "detail": {
                "role": role, "channel": row.get("channel"), "proactive": proactive,
                "unheard": bool(row.get("unheard")), "unwound": bool(row.get("unwound")),
                "text": clip_block(text, 2400),
                "image": selfie_url(row.get("image_url")),
                "selfie_id": row.get("selfie_id") or "",
            },
        })
        tally["chat"] += 1

    # -- the utility model ---------------------------------------------------
    for row in utility:
        t = when(row.get("timestamp"))
        if t is None:
            continue
        kind = row.get("kind") or "utility"
        counters["utility_kinds"][kind] += 1
        parsed = row.get("applied") or row.get("parsed") or []
        ops = [{"op": op.get("op"), "section": op.get("section"),
                "text": clip(op.get("text"), 260), "confidence": op.get("confidence")}
               for op in (parsed[:10] if isinstance(parsed, list) else [])
               if isinstance(op, dict)]
        quarantined = row.get("quarantined") or []
        events.append({
            "id": unique(f"util-{row['id']}" if row.get("id") else stable_id("util", row)),
            "t": t, "kind": "utility", "lane": "utility",
            "title": f"{kind} · {len(ops)} op{'s' if len(ops) != 1 else ''}",
            "summary": clip((ops[0]["text"] if ops else None) or row.get("raw_reply"), 280),
            "ref": "#/economics",
            "detail": {
                "kind": kind, "ops": ops, "applied": bool(row.get("applied")),
                "quarantined": ([clip(q, 200) for q in quarantined[:6]]
                                if isinstance(quarantined, list)
                                else ([clip(quarantined, 200)]
                                      if quarantined and quarantined is not True else [])),
                "exchange": clip_block(row.get("exchange"), 900),
            },
        })
        tally["utility"] += 1

    # -- her hands -----------------------------------------------------------
    for row in calls:
        t = when(row.get("ts"))
        if t is None:
            continue
        tool = row.get("tool") or "?"
        verdict = row.get("verdict") or "?"
        counters["tool_names"][tool] += 1
        counters["tool_verdicts"][verdict.split(":")[0]] += 1
        cid, tid = row.get("corr_id"), row.get("tick_id")
        call_id = unique(row.get("call_id") or stable_id("call", row))
        events.append({
            "id": call_id, "t": t, "kind": "tool", "lane": "tools",
            "title": f"{tool} · {verdict}",
            "summary": clip(row.get("args"), ARG_PREVIEW),
            "tick_id": tid, "corr_id": cid, "origin": row.get("origin"),
            "tool": tool, "verdict": verdict,
            "ref": f"#/tools/0?corr_id={cid}" if cid else "#/tools",
            "detail": {
                "tool": tool, "verdict": verdict, "origin": row.get("origin"),
                "duration_ms": row.get("duration_ms"),
                "args": small(row.get("args") or {}, 12),
                "result": clip_block(row.get("result"), 700),
            },
        })
        tally["tools"] += 1
        if tid:
            tools_by_tick[tid].append(call_id)
        if cid:
            by_corr[cid].append(call_id)
        session, index = row.get("session_id"), row.get("turn_index")
        if session and isinstance(index, int):
            tools_by_turn[(str(session)[:8], index)].append(call_id)

    # -- the camera ----------------------------------------------------------
    for row in selfies:
        t = when(row.get("created_at"))
        if t is None:
            continue
        sid = (row.get("selfie_id") or row.get("request_id") or row.get("image")
               or stable_id("shot", row))
        cid = row.get("corr_id")
        raw_template = row.get("template")
        template: dict = raw_template if isinstance(raw_template, dict) else {}
        ev_id = unique(f"selfie-{sid}")
        events.append({
            "id": ev_id, "t": t, "kind": "selfie", "lane": "selfies",
            "title": clip(template.get("look") or row.get("image") or "photo", 90),
            "summary": clip(row.get("prompt"), 280),
            "corr_id": cid,
            "ref": f"#/tools/0?corr_id={cid}" if cid else "#/tools",
            "detail": {
                "image": selfie_url(row.get("image")), "file": row.get("image"),
                "backend": row.get("backend"), "model": row.get("model"),
                "seed": row.get("seed"),
                "size": f"{row.get('width')}×{row.get('height')}" if row.get("width") else "",
                "look": clip(template.get("look"), 700),
                "prompt": clip(row.get("prompt"), 900),
            },
        })
        tally["selfies"] += 1
        if cid:
            by_corr[cid].append(ev_id)

    # -- the journal, and the Vault's history --------------------------------
    for ev in journal:
        ev["id"] = unique(ev["id"])
        events.append(ev)
        tally["journal"] += 1

    commit_tick: dict[str, str] = {}
    commit_turn: dict[str, tuple[str, int]] = {}
    for commit in commits:
        t = float(commit.get("at") or 0)
        sha = commit.get("sha") or ""
        subject = commit.get("subject") or "commit"
        files = commit.get("files") or []
        shown = ", ".join(f.get("path", "") for f in files[:4])
        if len(files) > 4:
            shown = f"{shown} +{len(files) - 4} more"
        ev_id = unique(f"commit-{sha[:12]}" if sha else stable_id("commit", commit))
        events.append({
            "id": ev_id, "t": t, "kind": "commit", "lane": "commits",
            "title": clip(subject, 110),
            "summary": shown or "no files",
            "ref": f"#/vault/commit/{sha}" if sha else "",
            "detail": {"sha": sha, "subject": subject,
                       "files": [{"path": f.get("path"), "added": f.get("added"),
                                  "deleted": f.get("deleted")} for f in files[:12]],
                       "more_files": max(0, len(files) - 12)},
        })
        tally["commits"] += 1
        if (m := _TICK_SUBJECT.match(subject)):
            commit_tick[ev_id] = m.group(1)
        elif (m := _TURN_SUBJECT.match(subject)):
            commit_turn[ev_id] = (m.group(1)[:8], int(m.group(2)))

    events.sort(key=lambda e: (e["t"], e["id"]))
    by_id = {e["id"]: e for e in events}
    times = [e["t"] for e in events]

    def window(t0: float, t1: float, kinds: set[str] | None = None) -> Iterator[Event]:
        for e in events[bisect_left(times, t0):bisect_right(times, t1)]:
            if kinds is None or e["kind"] in kinds:
                yield e

    # -- links ---------------------------------------------------------------
    links: list[dict] = []
    seen_link: set[tuple] = set()

    def link(a: str | None, b: str | None, rel: str,
             confidence: str = "explicit", reason: str = "") -> None:
        if not a or not b or a == b or a not in by_id or b not in by_id:
            return
        key = (*sorted((a, b)), rel)
        if key in seen_link:
            return
        seen_link.add(key)
        links.append({"a": a, "b": b, "rel": rel, "confidence": confidence,
                      "reason": reason or f"Recorded {rel} reference"})

    for tid in set(prompt_by_tick) | set(tools_by_tick):
        pids = prompt_by_tick.get(tid, [])
        cids = tools_by_tick.get(tid, [])
        for pid in pids:
            link(tid, pid, "tick")
        for cid in cids:
            link(tid, cid, "tick")
            # the model call that chose a hand precedes it
            before = [p for p in pids if cid in by_id
                      and by_id.get(p, {}).get("t", 0) <= by_id[cid]["t"]]
            if before:
                link(before[-1], cid, "tick")

    for ids in by_corr.values():
        ordered = sorted(ids, key=lambda i: by_id[i]["t"] if i in by_id else 0)
        for a, b in pairwise(ordered):
            link(a, b, "corr")

    for ev in events:
        if ev["kind"] == "tick":
            for sensed in ev["detail"]["sensed"]:
                link(sensed.get("id"), ev["id"], "sense")
        if ev.get("goal") and ev["kind"] in ("tick", "signal"):
            link(ev["goal"], ev["id"], "goal")
        if ev["kind"] == "chat" and ev.get("turn_id") in prompt_by_turn:
            # a reply ↔ the exact model call that wrote it
            link(prompt_by_turn[ev["turn_id"]], ev["id"], "turn")

    for ev_id, tid in commit_tick.items():
        link(tid, ev_id, "commit", reason="The commit message names this tick")
    for ev_id, turn in commit_turn.items():
        for call_id in tools_by_turn.get(turn, []):
            link(call_id, ev_id, "commit",
                 reason="The commit message names this call's session and turn")

    # a reach-out ↔ the call that composed it, and the tick that spoke it
    spoke_by: dict[str, list[str]] = {}
    for ev in events:
        if ev["kind"] != "chat" or not ev.get("proactive"):
            continue
        causes: list[str] = []
        evidence: dict[str, tuple[str, str]] = {}
        exact = by_id.get(prompt_by_turn.get(ev.get("turn_id") or "", ""))
        correlated = [by_id[p] for p in by_corr.get(ev.get("corr_id") or "", [])
                      if by_id[p]["kind"] == "prompt"
                      and by_id[p].get("prompt_kind") in REACH_OUT_PROMPTS]
        composed = [p for p in window(ev["t"] - REACH_OUT_WINDOW_S, ev["t"] + 2, {"prompt"})
                    if p.get("prompt_kind") in REACH_OUT_PROMPTS]
        prompt = exact or (correlated[-1] if correlated else None)
        explicit = prompt is not None
        prompt = prompt or (composed[-1] if composed else None)
        if prompt:
            causes.append(prompt["id"])
            evidence[prompt["id"]] = (
                ("explicit", "Matching turn_id / corr_id") if explicit
                else ("inferred", "Nearest composing call within 120 seconds"))
        tick = by_id.get(ev.get("tick_id") or "")
        tick_reason = ("explicit", "Message tick_id")
        if tick is None and prompt:
            tick = by_id.get(prompt.get("tick_id") or "")
            tick_reason = (("explicit", "Composing call tick_id") if explicit
                           else ("inferred", "tick_id of the inferred composing call"))
        if tick is None:
            candidates = [k for k in window(ev["t"], ev["t"] + SPOKE_AFTER_TICK_S, {"tick"})
                          if k["detail"]["acted"].get("what") in ("chat", "speak")]
            tick = candidates[0] if candidates else None
            tick_reason = ("inferred",
                           "First delivery tick recorded within 180 seconds after the message")
        if tick and tick["kind"] == "tick":
            causes.append(tick["id"])
            evidence[tick["id"]] = tick_reason
        for c in causes:
            link(c, ev["id"], "said", *evidence[c])
        ev["reach_evidence"] = {k: list(v) for k, v in evidence.items()}
        spoke_by[ev["id"]] = causes

    # A [she] line ↔ the tick that wrote it. The line is written during ACT and
    # the tick is traced after, so it belongs to the first tick at or after its
    # minute; failing that, the nearest one before.
    journal_by_tick: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        if ev["kind"] != "journal":
            continue
        minute = ev["t"] - ev["t"] % 60
        after = list(window(minute, minute + JOURNAL_AFTER_TICK_S, {"tick"}))
        near = after or list(window(minute - JOURNAL_AFTER_TICK_S, minute, {"tick"}))
        if near:
            tick = after[0] if after else near[-1]
            journal_by_tick[tick["id"]].append(ev["id"])
            link(tick["id"], ev["id"], "wrote", "inferred",
                 "Nearest tick around the journal's minute; no recorded tick_id")

    stories = build_stories(events, by_id, window, prompt_by_tick, tools_by_tick,
                            prompt_by_turn, spoke_by, goal_by_id, char_name, user_name,
                            journal_by_tick, act_threshold)
    for story in stories:
        inferred = story.setdefault("inferred_nodes", {})
        nodes = set(story["nodes"])
        for edge in links:
            if edge["confidence"] == "inferred" and edge["a"] in nodes and edge["b"] in nodes:
                node = edge["b"] if edge["a"] == story["anchor"] else edge["a"]
                inferred[node] = edge["reason"]

    context_rows = [{"t": t, "used": r.get("used"), "limit": r.get("limit"),
                     "pct": r.get("pct")}
                    for r in context if (t := when(r.get("timestamp"))) is not None]
    coverage = [*times, *(t for ts in rest_times.values() for t in ts),
                *(r["t"] for r in context_rows)]
    t0 = min(coverage) if coverage else now
    if since is not None:
        t0 = min(t0, since)
    last_acted = next((e for e in reversed(events) if e["kind"] == "tick"), None)
    last_chat = next((e for e in reversed(events) if e["kind"] == "chat"), None)

    def top(c: Counter, n: int = 24) -> list[dict]:
        return [{"name": k, "n": v} for k, v in c.most_common(n)]

    return {
        "meta": {
            "character": record.id, "name": char_name, "user": user_name,
            "generated_t": now,
            "window": {"days": days, "since": since, "until": now},
            "current_state": activity_now.get("state"),
            "cadence_s": activity_now.get("cadence_s"),
            "budget": budget, "situation": situation,
            "range": [t0, max(now, max(coverage) if coverage else now)],
            "act_threshold": act_threshold,
            "last_acted": last_acted["id"] if last_acted else None,
            "last_chat": last_chat["id"] if last_chat else None,
        },
        "stats": {
            **{k: tally.get(k, 0) for k in (
                "ticks", "rest", "acted", "chat", "utility", "tools", "prompts",
                "signals", "selfies", "journal", "activity", "commits")},
            **{k: top(c) for k, c in counters.items()},
            "tokens_in": tokens["in"], "tokens_out": tokens["out"],
            "events": len(events), "links": len(links), "stories": len(stories),
            "goals": len(goals),
            "open_goals": sum(1 for g in goals if g["state"] in ("pending", "active",
                                                                 "waiting")),
        },
        "goals": goals,
        "activity": activity_rows,
        "rest_density": rest_density,
        "events": events,
        "links": links,
        "stories": stories,
        "context": context_rows[-400:],
    }


# --- stories ------------------------------------------------------------------

def build_stories(events: list[Event], by_id: dict[str, Event],
                  window: Callable[..., Iterable[Event]],
                  prompt_by_tick: dict[str, list[str]], tools_by_tick: dict[str, list[str]],
                  prompt_by_turn: dict[str, str], spoke_by: dict[str, list[str]],
                  goal_by_id: dict[str, dict], char_name: str, user_name: str,
                  journal_by_tick: dict[str, list[str]], act_threshold: float) -> list[dict]:
    stories: list[dict] = []
    spoke_from_tick: dict[str, list[str]] = defaultdict(list)
    for chat_id, causes in spoke_by.items():
        for c in causes:
            if (by_id.get(c) or {}).get("kind") == "tick":
                spoke_from_tick[c].append(chat_id)

    # one decision: what she sensed → scored → chose → did → wrote down
    for ev in events:
        if ev["kind"] != "tick":
            continue
        # In the order the tick lived it, not by stamp: the tick is traced after
        # ACT, so its own model calls and hands carry earlier times.
        nodes: list[str] = []
        if ev.get("goal") in by_id:
            nodes.append(ev["goal"])
        nodes += [s["id"] for s in ev["detail"]["sensed"] if s.get("id") in by_id]
        nodes.append(ev["id"])
        nodes += prompt_by_tick.get(ev["id"], [])
        nodes += tools_by_tick.get(ev["id"], [])
        nodes += spoke_from_tick.get(ev["id"], [])
        nodes += journal_by_tick.get(ev["id"], [])
        stories.append({
            "id": f"story-{ev['id']}", "t": ev["t"], "kind": "tick",
            "title": ev["title"], "state": ev.get("state"), "bucket": ev.get("bucket"),
            "anchor": ev["id"], "nodes": list(dict.fromkeys(nodes)),
            "why": explain_tick(ev, goal_by_id, act_threshold),
        })

    # one exchange: what was said → the call that answered → hands → extraction
    chats = [e for e in events if e["kind"] == "chat"]
    i = 0
    while i < len(chats):
        ev = chats[i]
        if ev["role"] == "user":
            j = i + 1
            replies = []
            while j < len(chats) and chats[j]["role"] == "assistant" \
                    and not chats[j]["proactive"]:
                replies.append(chats[j])
                j += 1
            end = replies[-1]["t"] if replies else ev["t"]
            nodes = [ev["id"], *(r["id"] for r in replies)]
            inferred = {r["id"]: "Following reply in conversation order" for r in replies}
            exact = {prompt_by_turn.get(r.get("turn_id") or "") for r in replies}
            nodes += [p for p in exact if p]
            for other in window(ev["t"] - 5, end + 5, {"tool", "signal", "prompt", "selfie"}):
                if other["kind"] == "tool" and other.get("origin") not in ("chat_turn", None):
                    continue
                if other["kind"] == "signal" and other.get("signal_type") not in (
                        "user_message", "turn_committed"):
                    continue
                if other["kind"] == "prompt" and other.get("prompt_kind") not in (
                        "chat_turn", "knowledge"):
                    continue
                nodes.append(other["id"])
                inferred[other["id"]] = (
                    "Matching reply turn_id; reply associated by conversation order"
                    if other["id"] in exact
                    else "Nearby in time to this exchange; no explicit turn reference")
            for other in window(end, end + 180, {"utility", "signal"}):
                if other["kind"] == "signal" and other.get("signal_type") != "turn_committed":
                    continue
                nodes.append(other["id"])
                inferred[other["id"]] = ("Within 180 seconds after the exchange; "
                                         "no explicit turn reference")
            stories.append({
                "id": f"story-{ev['id']}", "t": ev["t"], "kind": "chat",
                "title": clip(ev["summary"], 110), "anchor": ev["id"],
                "nodes": list(dict.fromkeys(nodes)),
                "why": explain_chat(ev, replies, char_name, user_name),
                "inferred_nodes": inferred,
            })
            i = j
            continue
        if ev["proactive"]:
            causes = spoke_by.get(ev["id"], [])
            stories.append({
                "id": f"story-{ev['id']}", "t": ev["t"], "kind": "reach",
                "title": clip(ev["summary"], 110), "anchor": ev["id"],
                "nodes": list(dict.fromkeys([*causes, ev["id"]])),
                "why": explain_reach(ev, causes, by_id, char_name),
            })
        i += 1

    stories.sort(key=lambda s: s["t"], reverse=True)
    return stories


def explain_chat(user: Event, replies: list[Event], char_name: str, user_name: str) -> str:
    said = f"{spoken_when(user['t'])}, {user_name} said: “{clip(user['summary'], 180)}”"
    if not replies:
        return said + f" No reply from {char_name} is on record for this message."
    reply = replies[0]
    lag = reply["t"] - user["t"]
    more = (f" (and {len(replies) - 1} more message{'s' if len(replies) > 2 else ''})"
            if len(replies) > 1 else "")
    return (
        f"{said} That preempts the mind to ENGAGED from any state: the reactive body "
        f"answers on the turn pipeline, not on the tick cadence. {char_name} replied "
        f"{lag:.0f}s later{more}: “{clip(reply['summary'], 220)}” After the turn "
        f"commits, a turn_committed signal lands on the bus so REFLECT can fold promises "
        f"into goals, and the utility model may extract facts into USER.md."
    )


def explain_reach(ev: Event, causes: list[str], by_id: dict[str, Event],
                  char_name: str) -> str:
    bits = [f"{spoken_when(ev['t'])}, {char_name} sent a message marked proactive."]
    for c in causes:
        other = by_id.get(c)
        if not other:
            continue
        delta = other["t"] - ev["t"]
        ago = ("at about the same time" if abs(delta) < 2
               else f"{abs(delta):.0f}s {'after' if delta > 0 else 'before'} delivery")
        confidence, reason = (ev.get("reach_evidence") or {}).get(
            c, ("inferred", "Nearby timestamp"))
        label = (f"Inferred association ({reason})" if confidence == "inferred"
                 else f"Recorded association ({reason})")
        if other["kind"] == "tick":
            bits.append(f"{label}: a tick recorded {ago} chose «{clip(other['title'], 80)}».")
        elif other["kind"] == "prompt":
            bits.append(f"{label}: a {other.get('prompt_kind')} model call was recorded {ago}.")
    if not causes:
        bits.append("No composing call or tick sits close enough to name as the cause.")
    bits.append(f"What she said: “{clip(ev['summary'], 240)}”")
    return " ".join(bits)


# --- the cache ----------------------------------------------------------------

#: character → (signature, days, graph). One entry each: the page asks for one
#: window at a time, and a stale entry is rebuilt, never served.
_cache: dict[str, tuple[tuple, float | None, dict]] = {}
_cache_lock = threading.Lock()


def signature(record) -> tuple:
    """What the graph was built from, as (path, size, mtime) — plus the Vault's
    head, since a commit changes the graph without touching any log."""
    paths = [debug.source(record, name) for name in debug.SOURCES]
    paths += [p.with_name(p.name + ".1") for p in paths]
    vault = Path(record.paths.vault)
    paths += [vault / "state" / "conversation.jsonl", vault / "goals.md",
              vault / "state" / "activity.json"]
    episodic = vault / "memory" / "episodic"
    if episodic.is_dir():
        paths += sorted(episodic.glob("*.md"))[-2:]
    sig: list = []
    for p in paths:
        try:
            st = p.stat()
            sig.append((str(p), st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append((str(p), None, None))
    sig.append(("head", vaultgit.head(vault)))
    return tuple(sig)


def graph(record, *, days: float | None, **kwargs: Any) -> dict:
    """`build`, reused while nothing it read has changed. A windowed graph
    rebuilt a minute later would differ only at its trailing edge, so an
    unchanged signature serves the cached one for up to a minute."""
    sig = signature(record)
    with _cache_lock:
        hit = _cache.get(record.id)
    if hit and hit[0] == sig and hit[1] == days \
            and time.time() - hit[2]["meta"]["generated_t"] < 60:
        return hit[2]
    built = build(record, days=days, **kwargs)
    with _cache_lock:
        _cache[record.id] = (sig, days, built)
    return built
