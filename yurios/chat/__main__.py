"""A character-aware terminal client for a running YuriOS host.

``python -m yurios.chat`` is deliberately only a frontend: turns, greetings,
events, character cards, and live model changes all stay on the running host.
It works with both the multi-character host and the older standalone runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx

SESSION_FILE = Path.home() / ".cache" / "yurios" / "cli-sessions.json"
LEGACY_SESSION_FILE = Path.home() / ".cache" / "yurios" / "cli-session"
DEFAULT_URL = "http://127.0.0.1:8768"
RESET = "\033[0m"
PALETTE = {
    "title": "\033[1;38;5;213m",
    "accent": "\033[1;38;5;111m",
    "muted": "\033[38;5;245m",
    "success": "\033[38;5;114m",
    "warning": "\033[38;5;221m",
    "error": "\033[38;5;210m",
}


def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(kind: str, text: str) -> str:
    return f"{PALETTE[kind]}{text}{RESET}" if _color_enabled() else text


def session_key(character_id: str | None) -> str:
    return character_id or "__standalone__"


def load_sessions(fresh: bool) -> dict[str, str]:
    """Read per-character conversation ids, accepting the pre-0.2 single id."""
    if fresh:
        return {}
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(key): str(value) for key, value in raw.items() if value}
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    try:
        legacy = LEGACY_SESSION_FILE.read_text(encoding="utf-8").strip()
        return {session_key(None): legacy} if legacy else {}
    except OSError:
        return {}


def save_sessions(sessions: dict[str, str]) -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(sessions, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
    except OSError:
        pass  # A missing cache only starts a fresh conversation next time.


def saved_session(sessions: dict[str, str], character_id: str | None,
                  primary: str | None) -> str | None:
    """Get a card's session, carrying the old primary-only cache forward once."""
    key = session_key(character_id)
    current = sessions.get(key)
    if current or not character_id or character_id != primary:
        return current
    legacy = sessions.pop(session_key(None), None)
    if legacy:
        sessions[key] = legacy
        save_sessions(sessions)
    return legacy


def endpoint(state: dict[str, Any], resource: str) -> str:
    """Address the selected runtime without making the host's primary special."""
    character_id = state.get("character_id")
    if character_id:
        return f"/api/characters/{character_id}/{resource}"
    return f"/api/{resource}"


def response_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except (ValueError, AttributeError):
        detail = response.text
    return str(detail).strip() or f"HTTP {response.status_code}"


def shorten(value: str, width: int = 48) -> str:
    return value if len(value) <= width else value[: width - 3] + "..."


def message_lines(text: str, width: int | None = None) -> list[str]:
    available = width or max(48, min(shutil.get_terminal_size((92, 24)).columns - 8, 100))
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, width=available, replace_whitespace=False) or [""])
    return lines


def show(state: dict[str, Any], entry: dict[str, Any]) -> None:
    """Render a committed assistant message as a compact, readable card."""
    name = state["name"]
    reached_out = "  " + paint("muted", "[reached out]") if entry.get("proactive") else ""
    lead = "" if state["awaiting"] else "\r"
    print(f"{lead}{paint('title', name.upper())}{reached_out}")
    for line in message_lines(str(entry.get("text", ""))):
        print(f"  {paint('accent', '|')} {line}")
    if entry.get("image_url"):
        print(f"  {paint('muted', 'selfie:')} {state['url']}{entry['image_url']}")
    if not state["awaiting"]:
        print(prompt(state), end="", flush=True)


def prompt(state: dict[str, Any]) -> str:
    label = f"[{state['name']}]"
    return f"{paint('accent', label)} you > "


def print_banner(state: dict[str, Any]) -> None:
    name = state["name"]
    card = state.get("character_id") or "standalone runtime"
    print()
    print(paint("title", "Y U R I O S   T E R M I N A L"))
    print(paint("muted", "-" * 68))
    print(f"  {paint('accent', name)}  {paint('muted', '[' + card + ']')}")
    print(f"  {paint('muted', 'Commands:')} /cards  /use <id>  /model  /help")
    print()


def print_cards(cards: list[dict[str, Any]], primary: str | None) -> None:
    print()
    print(paint("title", "CHARACTER CARDS"))
    print(paint("muted", "-" * 68))
    for card in cards:
        marker = "*" if card["id"] == primary else " "
        status = card.get("state", "offline")
        status_kind = "success" if card.get("runtime_state") == "ready" else "warning"
        print(f"{marker} {paint('accent', card['id'])}  {card.get('name') or card['id']}")
        print(f"    {paint(status_kind, status)}  {paint('muted', shorten(str(card.get('model') or 'inherit')))}")
        description = str(card.get("description") or "").strip()
        if description:
            print(f"    {paint('muted', shorten(description, 72))}")
    print(paint("muted", "* default card; /use <id> changes rooms"))


async def discover_host(client: httpx.AsyncClient) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return the host registry, or None when talking to a standalone runtime."""
    try:
        response = await client.get("/api/characters", timeout=5)
    except httpx.HTTPError as exc:
        raise RuntimeError(str(exc)) from exc
    if response.status_code == 200:
        body = response.json()
        cards = body.get("characters")
        if isinstance(cards, list):
            return cards, body.get("primary")
    if response.status_code not in (404, 405):
        raise RuntimeError(response_detail(response))
    try:
        health = await client.get("/api/health", timeout=5)
    except httpx.HTTPError as exc:
        raise RuntimeError(str(exc)) from exc
    if health.status_code != 200:
        raise RuntimeError(response_detail(health))
    return None, None


async def choose_character(cards: list[dict[str, Any]], primary: str | None,
                           requested: str | None) -> str:
    if not cards:
        raise RuntimeError("the host has no character cards")
    ids = {str(card["id"]) for card in cards}
    if requested:
        if requested not in ids:
            raise RuntimeError(f"no character card named '{requested}'")
        return requested
    if len(cards) == 1:
        return str(cards[0]["id"])
    default = primary if primary in ids else str(cards[0]["id"])
    print_cards(cards, default)
    while True:
        choice = (await asyncio.to_thread(input, f"Choose a card [{default}]: ")).strip() or default
        if choice in ids:
            return choice
        print(paint("error", f"Unknown card '{choice}'. Enter one of: {', '.join(sorted(ids))}"))


async def check_character(client: httpx.AsyncClient, character_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/characters/{character_id}/health", timeout=5)
    if response.status_code != 200:
        raise RuntimeError(response_detail(response))
    return response.json()


async def start_listener(client: httpx.AsyncClient, state: dict[str, Any]) -> asyncio.Task:
    path = endpoint(state, "events")
    state["event_path"] = path
    return asyncio.create_task(listen(client, state, path), name=f"terminal-events-{state.get('character_id') or 'primary'}")


async def listen(client: httpx.AsyncClient, state: dict[str, Any], path: str) -> None:
    """Drain the selected character's event stream and print committed replies."""
    connected = False
    try:
        async with client.stream("GET", path, timeout=httpx.Timeout(None, connect=5)) as response:
            if response.status_code != 200:
                return
            connected = True
            state["sse"] = True
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                if event.get("type") == "hello":
                    state["name"] = event.get("character", state["name"])
                    continue
                if event.get("type") != "message" or event.get("role") != "assistant":
                    continue
                message_id = event.get("id")
                if not message_id or message_id in state["printed"]:
                    continue
                state["printed"].add(message_id)
                show(state, event)
    except (httpx.HTTPError, asyncio.CancelledError):
        pass
    finally:
        if state.get("event_path") == path:
            if connected and not state.get("switching"):
                print(paint("warning", "\r(events stream closed)"))
            state["sse"] = False


async def deliver(state: dict[str, Any], entry: dict[str, Any] | None) -> None:
    """Use the HTTP reply only when the event stream did not render it first."""
    if not entry:
        return
    for _ in range(20):
        if not state["sse"] or entry["id"] in state["printed"]:
            break
        await asyncio.sleep(0.1)
    if entry["id"] not in state["printed"]:
        state["printed"].add(entry["id"])
        show(state, entry)


def remember_session(state: dict[str, Any], sessions: dict[str, str], session_id: str | None) -> None:
    if not session_id:
        return
    state["session_id"] = session_id
    sessions[session_key(state.get("character_id"))] = session_id
    save_sessions(sessions)


async def greet(client: httpx.AsyncClient, state: dict[str, Any], sessions: dict[str, str]) -> None:
    state["awaiting"] = True
    try:
        response = await client.post(endpoint(state, "greeting"), json={
            "session_id": state["session_id"], "channel": "cli"}, timeout=180)
        if response.status_code != 200:
            print(paint("warning", f"(greeting unavailable: {response_detail(response)})"))
            return
        data = response.json()
        remember_session(state, sessions, data.get("session_id"))
        await deliver(state, data.get("message"))
    except httpx.HTTPError as exc:
        print(paint("warning", f"(greeting unavailable: {exc})"))
    finally:
        state["awaiting"] = False


async def show_models(client: httpx.AsyncClient, state: dict[str, Any]) -> None:
    if not state.get("character_id"):
        print(paint("warning", "Model controls require the multi-character YuriOS host."))
        return
    response = await client.get(f"/api/characters/{state['character_id']}/brain", timeout=10)
    if response.status_code != 200:
        print(paint("error", f"(cannot read model settings: {response_detail(response)})"))
        return
    effective = response.json()["effective"]
    print(f"  {paint('muted', 'chat:')}    {effective['chat_model']}")
    print(f"  {paint('muted', 'utility:')} {effective['utility_model']}")


async def set_model(client: httpx.AsyncClient, state: dict[str, Any], field: str, value: str) -> bool:
    if not state.get("character_id"):
        print(paint("warning", "Model controls require the multi-character YuriOS host."))
        return False
    response = await client.patch(f"/api/characters/{state['character_id']}/brain",
                                  json={field: "" if value == "reset" else value}, timeout=30)
    if response.status_code != 200:
        print(paint("error", f"(model change failed: {response_detail(response)})"))
        return False
    body = response.json()
    label = "chat" if field == "chat_model" else "utility"
    print(paint("success", f"{label} model now: {body['effective'][field]}"))
    return True


async def switch_character(client: httpx.AsyncClient, state: dict[str, Any], sessions: dict[str, str],
                           listener: asyncio.Task, character_id: str) -> asyncio.Task:
    try:
        health = await check_character(client, character_id)
    except RuntimeError as exc:
        print(paint("error", f"(cannot enter '{character_id}': {exc})"))
        return listener
    state["switching"] = True
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await listener
    state.update(character_id=character_id, name=health.get("character", character_id),
                 session_id=sessions.get(session_key(character_id)), printed=set(), switching=False)
    listener = await start_listener(client, state)
    print_banner(state)
    await greet(client, state, sessions)
    return listener


async def handle_command(command: str, client: httpx.AsyncClient, state: dict[str, Any],
                         sessions: dict[str, str], cards: list[dict[str, Any]] | None,
                         primary: str | None, listener: asyncio.Task) -> tuple[bool, asyncio.Task]:
    try:
        words = shlex.split(command)
    except ValueError as exc:
        print(paint("error", f"(invalid command: {exc})"))
        return True, listener
    verb = words[0].lower()
    if verb in ("/quit", "/exit"):
        return False, listener
    if verb == "/help":
        print("  /cards                 list character cards")
        print("  /use <id>              switch to a running card")
        print("  /model [id|reset]      show or change the reply model")
        print("  /utility [id|reset]    show or change the utility model")
        print("  /new                   start a fresh conversation for this card")
        print("  /clear                 clear the terminal")
        print("  /quit                  leave the room")
        return True, listener
    if verb in ("/cards", "/characters"):
        if cards is None:
            print(paint("warning", "This server exposes one standalone runtime."))
        else:
            print_cards(cards, primary)
        return True, listener
    if verb == "/use":
        if cards is None:
            print(paint("warning", "This server exposes one standalone runtime."))
        elif len(words) != 2:
            print(paint("warning", "Usage: /use <character-id>"))
        elif words[1] not in {str(card["id"]) for card in cards}:
            print(paint("error", f"No card named '{words[1]}'. Use /cards to list them."))
        elif words[1] == state.get("character_id"):
            print(paint("muted", "Already in that room."))
        else:
            listener = await switch_character(client, state, sessions, listener, words[1])
        return True, listener
    if verb in ("/model", "/utility"):
        field = "chat_model" if verb == "/model" else "utility_model"
        if len(words) == 1:
            await show_models(client, state)
        elif len(words) == 2:
            await set_model(client, state, field, words[1])
        else:
            print(paint("warning", f"Usage: {verb} [model-id|reset]"))
        return True, listener
    if verb == "/new":
        sessions.pop(session_key(state.get("character_id")), None)
        save_sessions(sessions)
        state["session_id"] = None
        print(paint("success", "Started a fresh conversation."))
        await greet(client, state, sessions)
        return True, listener
    if verb == "/clear":
        print("\033[2J\033[H" if _color_enabled() else "\n" * 4, end="")
        print_banner(state)
        return True, listener
    print(paint("warning", "Unknown command. Type /help for available controls."))
    return True, listener


async def send_turn(client: httpx.AsyncClient, state: dict[str, Any],
                    sessions: dict[str, str], text: str) -> dict[str, Any] | None:
    """One HTTP turn on the cli channel. Returns the committed assistant entry."""
    response = await client.post(endpoint(state, "chat"), json={
        "text": text, "session_id": state["session_id"], "channel": "cli"},
                                 timeout=180)
    if response.status_code != 200:
        raise RuntimeError(response_detail(response))
    data = response.json()
    remember_session(state, sessions, data.get("session_id"))
    return data.get("message")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m yurios.chat",
        description="character-aware terminal chat for a running YuriOS host")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="the server origin (default: %(default)s)")
    parser.add_argument("--token", default=os.environ.get("YURIOS_OWNER_TOKEN", ""),
                        help="remote owner token (prefer YURIOS_OWNER_TOKEN to command history)")
    parser.add_argument("character_id", nargs="?",
                        help="character card to enter (same as --character)")
    parser.add_argument("--character", help="character card to enter")
    parser.add_argument("-m", "--message",
                        help="send one turn and exit instead of opening a room")
    parser.add_argument("--model", help="set the selected card's reply model before chatting")
    parser.add_argument("--utility-model", help="set the selected card's utility model before chatting")
    parser.add_argument("--new", action="store_true", help="start a fresh conversation for this card")
    args = parser.parse_args(argv)
    requested = args.character_id or args.character
    url = args.url.rstrip("/")
    sessions = load_sessions(args.new)
    state: dict[str, Any] = {"name": "her", "sse": False, "printed": set(), "url": url,
                             "awaiting": False, "switching": False, "character_id": None,
                             "session_id": None, "event_path": ""}

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient(base_url=url, headers=headers) as client:
        try:
            cards, primary = await discover_host(client)
            if cards is None:
                health = (await client.get("/api/health", timeout=5)).json()
                state["name"] = health.get("character", state["name"])
            else:
                character_id = await choose_character(cards, primary, requested)
                health = await check_character(client, character_id)
                state["character_id"] = character_id
                state["name"] = health.get("character", character_id)
        except (RuntimeError, httpx.HTTPError, ValueError) as exc:
            print(paint("error", f"No reachable YuriOS at {url}: {exc}"))
            print("Start it with: yurios start")
            return 1

        state["session_id"] = saved_session(sessions, state.get("character_id"), primary)
        if args.model:
            await set_model(client, state, "chat_model", args.model)
        if args.utility_model:
            await set_model(client, state, "utility_model", args.utility_model)

        if args.message is not None:
            state["awaiting"] = True
            try:
                entry = await send_turn(client, state, sessions, args.message)
            except (RuntimeError, httpx.HTTPError) as exc:
                print(paint("error", f"(turn failed: {exc})"))
                return 1
            if entry:
                show(state, entry)
            return 0

        listener = await start_listener(client, state)
        print_banner(state)
        await greet(client, state, sessions)

        try:
            running = True
            while running:
                try:
                    text = (await asyncio.to_thread(input, prompt(state))).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not text:
                    continue
                if text.startswith("/"):
                    running, listener = await handle_command(text, client, state, sessions,
                                                             cards, primary, listener)
                    continue
                state["awaiting"] = True
                try:
                    try:
                        entry = await send_turn(client, state, sessions, text)
                    except httpx.HTTPError as exc:
                        print(paint("error", f"(send failed: {exc})"))
                        continue
                    except RuntimeError as exc:
                        print(paint("error", f"(turn failed: {exc})"))
                        continue
                    await deliver(state, entry)
                finally:
                    state["awaiting"] = False
        finally:
            state["switching"] = True
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
    print(paint("muted", "left the room."))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
