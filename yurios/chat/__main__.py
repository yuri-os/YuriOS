"""`python -m yurios.chat [--url http://127.0.0.1:8768]` — talk to her from a
terminal (SPEC §10.5).

A thin client, per the frontend rule: it drives the host's HTTP surface and
holds no brain of its own. Two wires:

  - **POST /api/chat** sends your line as one text turn (channel `cli`);
  - **GET /api/events** (SSE) is where her words come from — the committed
    reply, and anything she says *unprompted*: a greeting on the web page, a
    reply to a Telegram message, the mind's reach-out. The terminal is a
    window into the same one room every other frontend shows.

With the stream up, replies print from it and the POST result is only
bookkeeping (dedup by message id); if the stream can't connect, replies fall
back to printing from the POST response — a degraded but working conversation.

The session id persists in ~/.cache/yurios/cli-session so the conversation
window survives across runs (`--new` starts a fresh one). An attached terminal
counts as presence exactly like an open page — the /api/events subscription
posts `user_present` server-side, so the mind knows you're in the room.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

import httpx

SESSION_FILE = Path.home() / ".cache" / "yurios" / "cli-session"
PROMPT = "you › "


def load_session(fresh: bool) -> str | None:
    if fresh:
        return None
    try:
        sid = SESSION_FILE.read_text(encoding="utf-8").strip()
        return sid or None
    except OSError:
        return None


def save_session(session_id: str) -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(session_id, encoding="utf-8")
    except OSError:
        pass                                   # a lost session is a fresh window, not an error


def show(state: dict, entry: dict) -> None:
    """Print one of her lines. While a turn is in flight (`awaiting`) the
    prompt isn't on screen and the loop prints the next one itself; otherwise
    the line arrived unsolicited over the pending `input()` prompt — overwrite
    it and redraw, so a proactive reach-out doesn't eat the prompt."""
    mark = " (she reached out)" if entry.get("proactive") else ""
    lead = "" if state["awaiting"] else "\r"
    print(f"{lead}{state['name']} › {entry.get('text', '')}{mark}")
    if entry.get("image_url"):
        print(f"        [selfie: {state['url']}{entry['image_url']}]")
    if not state["awaiting"]:
        print(PROMPT, end="", flush=True)      # redraw the prompt input() holds


async def listen(client: httpx.AsyncClient, state: dict) -> None:
    """Drain /api/events; print her committed lines. Sets state['sse'] so the
    POST path knows whether to print replies itself."""
    try:
        async with client.stream(
                "GET", "/api/events",
                timeout=httpx.Timeout(None, connect=5)) as resp:
            state["sse"] = True
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                if event.get("type") == "hello":
                    state["name"] = event.get("character", state["name"])
                    continue
                if event.get("type") != "message":
                    continue
                if event.get("role") != "assistant":
                    continue
                if event.get("id") in state["printed"]:
                    continue
                state["printed"].add(event.get("id"))
                show(state, event)
    except (httpx.HTTPError, asyncio.CancelledError):
        pass
    finally:
        if state["sse"]:
            print("\r(events stream closed)")
        state["sse"] = False


async def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m yurios.chat",
        description="terminal chat with a running YuriOS (python -m yurios.world)")
    ap.add_argument("--url", default="http://127.0.0.1:8768",
                    help="the server origin (default: %(default)s)")
    ap.add_argument("--new", action="store_true",
                    help="start a fresh conversation window")
    args = ap.parse_args()
    url = args.url.rstrip("/")

    session_id = load_session(args.new)
    state = {"name": "her", "sse": False, "printed": set(), "url": url,
             "awaiting": False}    # True while a turn is in flight (no prompt on screen)

    async with httpx.AsyncClient(base_url=url) as client:
        try:
            health = (await client.get("/api/health", timeout=5)).json()
            state["name"] = health.get("character", state["name"])
        except httpx.HTTPError as e:
            print(f"no YuriOS at {url} ({e}) — start it: python -m yurios.world")
            return 1

        listener = asyncio.create_task(listen(client, state))
        print(f"connected to {url} — she's listening. /quit to leave.")
        try:
            while True:
                try:
                    text = await asyncio.to_thread(input, PROMPT)
                except (EOFError, KeyboardInterrupt):
                    break
                text = text.strip()
                if not text:
                    continue
                if text in ("/quit", "/exit"):
                    break
                state["awaiting"] = True
                try:
                    try:
                        resp = await client.post(
                            "/api/chat",
                            json={"text": text, "session_id": session_id,
                                  "channel": "cli"},
                            timeout=180)
                    except httpx.HTTPError as e:
                        print(f"(send failed: {e})")
                        continue
                    if resp.status_code != 200:
                        detail = resp.json().get("detail", resp.text)
                        print(f"(turn failed: {detail})")
                        continue
                    data = resp.json()
                    session_id = data["session_id"]
                    save_session(session_id)
                    entry = data.get("message")
                    if entry:
                        # the stream is the display path; give it a moment to
                        # show the commit, then print it ourselves (stream
                        # down or slow)
                        for _ in range(20):
                            if not state["sse"] or entry["id"] in state["printed"]:
                                break
                            await asyncio.sleep(0.1)
                        if entry["id"] not in state["printed"]:
                            state["printed"].add(entry["id"])
                            show(state, entry)
                finally:
                    state["awaiting"] = False
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
    print("left the room.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
