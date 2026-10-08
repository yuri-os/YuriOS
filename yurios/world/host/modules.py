"""The files a character carries that travel as modules (SPEC §11.4).

Two of her behaviours are files rather than settings, and both are worth
moving between characters and installations whole:

- **Night jobs** — `vault/dreams/<name>.md`, YAML frontmatter over the prompt
  she is given at night (§21.2). The runtime's own routes (`/api/mind/dream/
  jobs`, world/routes/mind.py) edit them only while she is running; these work
  on the file either way, so a parked character's night can be prepared before
  she is approved, and a job can be exported from one character and imported
  into another — singly as its `.md`, or as a zip of all of them.
- **Selfie scene libraries** — her `selfie.yaml` (§7.6). The studio edits the
  rows; this adds the file as a unit: export it, and parse a YAML file someone
  handed you into rows the studio's PUT accepts, to replace hers or merge in.

Validation is the runner's (`validate_job_file`) and the library's
(`selfiebook.parse_text`), never a second opinion: a file this accepts is one
the night and the camera will load.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
# the form parser's class: FastAPI's UploadFile is a subclass it never builds
from starlette.datastructures import UploadFile

from yurios.characters import CharacterRecord, selfiebook
from yurios.mind.dreamjobs import (BUILTIN_NAMES, JOB_KINDS, JOB_NAME_RE,
                                   DreamRunner, load_job_files,
                                   validate_job_file)

from .hosting import CharacterHost

log = logging.getLogger("world.host")

#: One upload — a zip of job files is a few kilobytes; anything near this is
#: not a set of prompts.
MAX_IMPORT_BYTES = 2 * 1024 * 1024
# `[ \t]*`, not `\s*`: a `\s` after the closing fence would swallow the blank
# line between the frontmatter and the prompt, and a rename would rewrite more
# of the file than its one `name:` line.
_FRONT = re.compile(r"\A---[ \t]*\n(?P<front>.*?)\n---[ \t]*\n", re.S)
_NAME_LINE = re.compile(r"^name:.*$", re.M)


def with_name(text: str, name: str) -> str:
    """The same job file, its frontmatter `name:` set to `name`.

    A job is filed under the name its frontmatter declares and the runner
    refuses a file whose two names disagree — so importing `diary.md` as
    `night-diary` rewrites that one line and nothing else in the file."""
    match = _FRONT.match(text)
    if not match:
        return text
    front = match.group("front")
    front = (_NAME_LINE.sub(f"name: {name}", front, count=1) if _NAME_LINE.search(front)
             else f"name: {name}\n{front}")
    return f"---\n{front}\n---\n" + text[match.end():]


def is_job_file(filename: str) -> bool:
    """The loader's own rule (`load_job_files`): a folder's README is its
    documentation, and a dotfile is nobody's — neither is a job, so neither is
    exported, and an import of a zip that carries one passes over it quietly."""
    name = Path(filename).name
    return (name.lower().endswith(".md") and not name.startswith(".")
            and Path(name).stem.lower() != "readme")


def first_line(reason: str) -> str:
    """The sentence of a refusal, without the worked example the runner appends
    for its own editor — one line per refused file is what a status line holds."""
    return reason.split("\n", 1)[0].rstrip(": ")


def declared_name(text: str, fallback: str) -> str:
    match = _FRONT.match(text)
    found = _NAME_LINE.search(match.group("front")) if match else None
    name = found.group(0).split(":", 1)[1].strip().strip("'\"") if found else ""
    return name or fallback


def register(app: FastAPI, host: CharacterHost, require) -> None:

    def jobs_dir(record: CharacterRecord) -> Path:
        root = Path(record.paths.vault) / DreamRunner.JOBS_DIR
        root.mkdir(parents=True, exist_ok=True)
        return root

    def job_path(record: CharacterRecord, name: str) -> Path:
        # the only place a name becomes a path, and it is built, never taken
        if not JOB_NAME_RE.match(name or ""):
            raise HTTPException(400, "a job name is lowercase letters, digits, - and _ "
                                     "(it becomes vault/dreams/<name>.md)")
        return jobs_dir(record) / f"{name}.md"

    def her_mind(record: CharacterRecord):
        rt = host.runtime(record.id)
        return getattr(rt, "mind", None) if rt is not None else None

    async def commit(record: CharacterRecord, message: str) -> None:
        """A person did this, so it is filed now (AGENTS: the line is what the
        message names) — through her running vault when she has one."""
        mind = her_mind(record)
        try:
            if mind is not None:
                mind.vault.mark_dirty()
                await asyncio.to_thread(mind.vault.commit_if_dirty, message, now=True)
            else:
                from yurios.app import vaultgit
                await asyncio.to_thread(vaultgit.commit, Path(record.paths.vault),
                                        message, now=True)
        except Exception:                       # noqa: BLE001 - the write already landed
            log.exception("could not commit %s for %s", message, record.id)

    def reload(record: CharacterRecord) -> None:
        mind = her_mind(record)
        if mind is not None and getattr(mind, "dreams", None) is not None:
            mind.dreams.reload()

    async def write_job(record: CharacterRecord, name: str, text: str) -> None:
        problem = validate_job_file(name, text)
        if problem:
            raise HTTPException(422, problem)
        path = job_path(record, name)
        mind = her_mind(record)
        if mind is not None:
            mind.vault.write(f"{DreamRunner.JOBS_DIR}/{name}.md", text)
        else:
            path.write_text(text, encoding="utf-8")

    # --- night jobs -------------------------------------------------------------

    @app.get("/api/characters/{character_id}/dream-jobs")
    async def dream_jobs(character_id: str):
        record = require(character_id)
        root = jobs_dir(record)
        jobs = []
        for spec in load_job_files(root):
            text = (root / f"{spec.name}.md").read_text(encoding="utf-8")
            jobs.append({"name": spec.name, "front": spec.front, "text": text,
                         "builtin": spec.name in BUILTIN_NAMES})
        return {"character": record.id, "name": record.display.name,
                "running": her_mind(record) is not None,
                "jobs": jobs, "kinds": sorted(JOB_KINDS), "builtins": sorted(BUILTIN_NAMES)}

    @app.put("/api/characters/{character_id}/dream-jobs/{name}")
    async def save_dream_job(character_id: str, name: str, request: Request):
        record = require(character_id)
        body = await request.json()
        text = body.get("text") if isinstance(body, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(422, "text must be the whole file: YAML frontmatter "
                                     "between --- lines, then the prompt body")
        await write_job(record, name, text)
        await commit(record, f"dreams: edited {name}")
        reload(record)
        return {"name": name, "text": text}

    @app.delete("/api/characters/{character_id}/dream-jobs/{name}")
    async def delete_dream_job(character_id: str, name: str):
        record = require(character_id)
        path = job_path(record, name)
        if not path.is_file():
            raise HTTPException(404, f"no job file called {name}")
        path.unlink()
        await commit(record, f"dreams: removed {name}")
        reload(record)
        return {"name": name, "deleted": True, "reverted": name in BUILTIN_NAMES}

    @app.get("/api/characters/{character_id}/dream-jobs/export")
    async def export_dream_jobs(character_id: str):
        """Every job file she has, as one zip — the night as a module."""
        record = require(character_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(jobs_dir(record).glob("*.md")):
                if is_job_file(path.name):
                    archive.writestr(path.name, path.read_text(encoding="utf-8"))
        return Response(buffer.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{record.id}-night-jobs.zip"'})

    @app.post("/api/characters/{character_id}/dream-jobs/import")
    async def import_dream_jobs(character_id: str, request: Request):
        """`.md` job files, or zips of them, into her night.

        A file is filed under the name its frontmatter declares (or its own
        filename when it declares none). One that is already hers is skipped
        unless `overwrite` is set; one the runner would refuse is reported with
        the runner's own sentence. Each file stands alone — one bad job in a zip
        does not cost the others."""
        record = require(character_id)
        form = await request.form()
        overwrite = str(form.get("overwrite") or "").lower() in ("1", "true", "on")
        files: list[tuple[str, str]] = []
        for upload in form.getlist("files"):
            if not isinstance(upload, UploadFile):
                continue
            data = await upload.read(MAX_IMPORT_BYTES + 1)
            if len(data) > MAX_IMPORT_BYTES:
                raise HTTPException(413, f"{upload.filename} is larger than an import may be")
            filename = Path(upload.filename or "job.md").name
            if filename.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        for info in archive.infolist():
                            if is_job_file(info.filename) and not info.is_dir():
                                files.append((Path(info.filename).name,
                                              archive.read(info).decode("utf-8", "replace")))
                except zipfile.BadZipFile:
                    raise HTTPException(422, f"{filename} is not a zip file") from None
            elif is_job_file(filename):
                files.append((filename, data.decode("utf-8", "replace")))
        if not files:
            raise HTTPException(422, "no .md job files in that upload")
        imported, skipped, refused = [], [], []
        for filename, text in files:
            name = declared_name(text, Path(filename).stem.lower())
            text = with_name(text, name)
            if not JOB_NAME_RE.match(name):
                refused.append({"file": filename, "reason": f"{name!r} is not a job name"})
                continue
            if job_path(record, name).is_file() and not overwrite:
                skipped.append(name)
                continue
            try:
                await write_job(record, name, text)
            except HTTPException as exc:
                refused.append({"file": filename, "reason": first_line(str(exc.detail))})
                continue
            imported.append(name)
        if imported:
            await commit(record, f"dreams: imported {', '.join(imported)}")
            reload(record)
        return {"imported": imported, "skipped": skipped, "refused": refused}

    # --- selfie scene libraries -----------------------------------------------------

    @app.get("/api/characters/{character_id}/selfie-templates/export")
    async def export_selfie_templates(character_id: str):
        record = require(character_id)
        book, _source = selfiebook.read_for(record)
        return Response(selfiebook.render_yaml(book, record.display.name),
                        media_type="application/x-yaml", headers={
                            "Content-Disposition":
                                f'attachment; filename="{record.id}-selfie.yaml"'})

    @app.post("/api/selfie-templates/parse")
    async def parse_selfie_templates(request: Request):
        """A YAML library as the studio's rows — for an import to replace hers or
        merge into it through the ordinary PUT. Refuses what the camera would."""
        body = await request.json()
        text = body.get("text") if isinstance(body, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(422, "send the YAML file's text")
        try:
            book = selfiebook.parse_text(text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not any(book["slots"].values()):
            raise HTTPException(422, "that file names no scenes, framings, lighting, "
                                     "moods or wardrobe")
        return {"book": book}
