"""What she did, and why — read off the files (SPEC §24.3, §33).

The journal is her account of the night; everything under `/debug/*` is the
machine's. Ticks with the scores that decided them, every prompt any model was
handed, the tool audit joined to the photo each call produced, the Vault's git
history, the recall index, the day's spend.

All of it reads files rather than a running mind, which is the point: the
character who just crashed is the one you can still take apart.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException

from yurios.app import vaultgit
from yurios.mind.journal import canonical_day, is_canonical_day, parse_day_entries

from .. import debug
from .hosting import (JOURNAL_PAGE_SIZE, CharacterHost, _log_sort_key, _tail_jsonl)

log = logging.getLogger("world.host")


def register(app: FastAPI, host: CharacterHost, require) -> None:
    """Declare this module's routes on the host app.

    A plain closure rather than an `APIRouter` because these routes read the
    host and the registry out of the enclosing scope the way they always did,
    and rebinding them here keeps the bodies byte-identical to the single
    function they were extracted from. Declaration order is the order these
    `register` calls run in, which matters: every explicit route has to be on
    the app before `create_host_app` mounts the runtime dispatcher over
    `/api/characters`.
    """
    @app.get("/api/characters/{character_id}/journal")
    async def journal(character_id: str, page: int = 0, day: str | None = None):
        """The diary index (paged 20 days at a time, newest day first) or,
        with `day=YYYY-MM-DD`, that one day's entries newest-first. Reads the
        episodic files straight off disk (like /log and /context-history) so
        history is visible whether or not the mind loop is currently running."""
        record = require(character_id)
        episodic = Path(record.paths.vault) / "memory" / "episodic"
        if day:
            try:
                day = canonical_day(day)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            path = episodic / f"{day}.md"
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            entries = parse_day_entries(text)
            entries.reverse()
            return {"day": day, "entries": entries}
        all_days = sorted((p.stem for p in episodic.glob("*.md")
                           if is_canonical_day(p.stem)), reverse=True) \
            if episodic.is_dir() else []
        page = max(0, page)
        start = page * JOURNAL_PAGE_SIZE
        page_days = all_days[start:start + JOURNAL_PAGE_SIZE]
        out = [{"day": d, "count": len(parse_day_entries((episodic / f"{d}.md").read_text(encoding="utf-8")))}
               for d in page_days]
        return {"days": out, "page": page,
                "has_more": start + JOURNAL_PAGE_SIZE < len(all_days), "total": len(all_days)}

    @app.get("/api/characters/{character_id}/log")
    async def logs(character_id: str):
        record = require(character_id)
        rows = _tail_jsonl(record.paths.traces / "ticks.jsonl", 60)
        rows += _tail_jsonl(record.paths.tool_logs / "calls.jsonl", 40)
        rows.sort(key=_log_sort_key)
        return {"entries": rows[-100:]}

    @app.get("/api/characters/{character_id}/context-history")
    async def context(character_id: str):
        record = require(character_id)
        rt = host.runtime(character_id)
        return {"context": rt.context.snapshot() if rt else {"used": 0, "limit": None},
                "history": _tail_jsonl(record.paths.traces / "context.jsonl", 500)}

    @app.get("/api/characters/{character_id}/debug/overview")
    async def debug_overview(character_id: str):
        # …and the five below: these read the Vault's git history, which is a
        # subprocess per call. A debug page is not worth stalling every
        # character on the node for, so they answer from a worker thread.
        return await asyncio.to_thread(
            debug.overview, require(character_id), host.runtime(character_id))

    @app.get("/api/characters/{character_id}/debug/activity")
    async def debug_activity(character_id: str, page: int = 0, limit: int = 100):
        return debug.activity(require(character_id), page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/ticks")
    async def debug_ticks(character_id: str, page: int = 0, limit: int = 25,
                          state: str | None = None, q: str | None = None):
        return debug.ticks(require(character_id), page=page, limit=limit,
                           state=state, q=q)

    @app.get("/api/characters/{character_id}/debug/ticks/{tick_id}")
    async def debug_tick(character_id: str, tick_id: str):
        found = debug.tick_detail(require(character_id), tick_id)
        if found is None:
            raise HTTPException(404, "no such tick in the live trace")
        return found

    @app.get("/api/characters/{character_id}/debug/signals")
    async def debug_signals(character_id: str, page: int = 0, limit: int = 100,
                            type: str | None = None):
        return debug.signals(require(character_id), page=page, limit=limit, type=type)

    @app.get("/api/characters/{character_id}/debug/goals")
    async def debug_goals(character_id: str):
        return debug.goals(require(character_id))

    @app.get("/api/characters/{character_id}/debug/self-edits")
    async def debug_self_edits(character_id: str):
        return await asyncio.to_thread(debug.self_edits, require(character_id))

    @app.get("/api/characters/{character_id}/debug/calls")
    async def debug_calls(character_id: str, page: int = 0, limit: int = 50,
                          tool: str | None = None, verdict: str | None = None,
                          corr_id: str | None = None):
        return debug.calls(require(character_id), page=page, limit=limit,
                           tool=tool, verdict=verdict, corr_id=corr_id)

    @app.get("/api/characters/{character_id}/debug/selfies")
    async def debug_selfies(character_id: str, page: int = 0, limit: int = 24):
        return debug.selfies(require(character_id), page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/prompts/days")
    async def debug_prompt_days(character_id: str, page: int = 0, limit: int = 20):
        return debug.prompt_days(require(character_id), page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/prompts")
    async def debug_prompts(character_id: str, day: str | None = None,
                            kind: str | None = None, page: int = 0,
                            limit: int = 25):
        return debug.prompts(require(character_id), day=day, kind=kind,
                             page=page, limit=limit)

    @app.get("/api/characters/{character_id}/debug/prompts/{prompt_id}")
    async def debug_prompt(character_id: str, prompt_id: str):
        found = debug.prompt_detail(require(character_id), prompt_id)
        if found is None:
            raise HTTPException(404, "no such prompt in the live log")
        return found

    @app.get("/api/characters/{character_id}/debug/vault/commits")
    async def debug_commits(character_id: str, page: int = 0, limit: int = 25,
                            path: str | None = None):
        return await asyncio.to_thread(
            debug.vault_commits, require(character_id), page=page,
            limit=limit, path=path)

    @app.get("/api/characters/{character_id}/debug/vault/commits/{sha}")
    async def debug_commit(character_id: str, sha: str):
        record = require(character_id)
        if not vaultgit.is_rev(sha):
            raise HTTPException(400, "not a commit id")
        found = await asyncio.to_thread(vaultgit.show, Path(record.paths.vault), sha)
        if found is None:
            raise HTTPException(404, "no such commit")
        return found

    @app.get("/api/characters/{character_id}/debug/vault/tree")
    async def debug_tree(character_id: str, path: str = ""):
        record = require(character_id)
        entries = vaultgit.tree(Path(record.paths.vault), path)
        if entries is None:
            raise HTTPException(400, "not a directory inside this vault")
        return {"path": path, "entries": entries}

    @app.get("/api/characters/{character_id}/debug/vault/file")
    async def debug_file(character_id: str, path: str, rev: str | None = None):
        record = require(character_id)
        found = await asyncio.to_thread(vaultgit.read_at,
                                        Path(record.paths.vault), path, rev=rev)
        if found is None:
            raise HTTPException(400, "not a readable file inside this vault")
        return found

    @app.get("/api/characters/{character_id}/debug/vault/history")
    async def debug_file_history(character_id: str, path: str, limit: int = 25):
        record = require(character_id)
        if vaultgit.in_vault(Path(record.paths.vault), path) is None:
            raise HTTPException(400, "not a path inside this vault")
        items = await asyncio.to_thread(vaultgit.log_records,
                                        Path(record.paths.vault),
                                        limit=max(1, min(limit, 200)), path=path)
        return {"path": path, "items": items}

    @app.get("/api/characters/{character_id}/debug/memory")
    async def debug_memory(character_id: str):
        return debug.memory(require(character_id))

    @app.get("/api/characters/{character_id}/debug/memory/chunks")
    async def debug_chunks(character_id: str, page: int = 0, limit: int = 50,
                           kind: str | None = None, q: str | None = None):
        return debug.chunks(require(character_id), page=page, limit=limit,
                            kind=kind, q=q)

    @app.get("/api/characters/{character_id}/debug/memory/chunks/{chunk_id}")
    async def debug_chunk(character_id: str, chunk_id: str):
        found = debug.chunk(require(character_id), chunk_id)
        if found is None:
            raise HTTPException(404, "no such chunk in the index")
        return found

    @app.get("/api/characters/{character_id}/debug/economics")
    async def debug_economics(character_id: str):
        return debug.economics(require(character_id))

    @app.get("/api/characters/{character_id}/debug/utility")
    async def debug_utility(character_id: str, page: int = 0, limit: int = 25,
                            kind: str | None = None):
        return debug.utility(require(character_id), page=page, limit=limit, kind=kind)
