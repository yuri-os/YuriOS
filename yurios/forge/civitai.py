"""Checkpoints from Civitai for her local camera (SPEC §11.5).

Her local camera reads three checkpoint families — SDXL, Krea 2 and Anima — and
inside each family only some files: the krea2 backend runs one INT4 layout, the
anima backend builds one size of transformer. Civitai lists thousands of
checkpoints and says neither. This module is the part of House settings'
**Checkpoints** page that knows both sides:

- **The catalog.** For every base model the camera can load, the most-downloaded
  checkpoints of all time and of this week, each sorted into what this build
  will do with it (`classify`): load it as-is, load it with a caveat, possibly
  load it (a Krea 2 INT4 file whose layout the listing doesn't name), or not at
  all. NSFW models are fetched too; whether they are *shown* is the page's
  switch. The answer is cached as one JSON file, because it is fourteen API
  calls and changes by the day, not the minute.
- **Downloads.** A file is fetched on a daemon thread, not the event loop — the
  host is one process holding every character's room, and a 7 GB stream is
  minutes of I/O — into `<name>.part`, resumed from where it stopped if the
  server dies under it, and only renamed once complete. Then its header is
  checked (`sniff.verify_local_checkpoint`), which settles what the listing
  could only guess.

The API key is read by the caller and passed in; nothing here reads `.env` or
logs a header that carries it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from yurios.forge.backends.sniff import read_safetensors_header, verify_local_checkpoint

log = logging.getLogger("forge.civitai")

API = "https://civitai.com/api/v1"
SITE = "https://civitai.com"
#: How old a cached catalog may be before the page fetches a fresh one.
CATALOG_TTL_S = 24 * 3600
#: Parallel listing calls — enough to finish in seconds, few enough to be polite.
FETCH_CONCURRENCY = 4
CHUNK = 1 << 20
#: Civitai's image ratings: 1 PG, 2 PG-13, 4 R, 8 X, 16 XXX, 32 blocked.
SFW_IMAGE_LEVEL = 2
IMAGE_WIDTH = 450

#: Tests replace this with an httpx.MockTransport; None is the network.
TRANSPORT: Optional[httpx.BaseTransport] = None
ASYNC_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


@dataclass(frozen=True)
class Family:
    base_model: str          # Civitai's baseModel name
    label: str               # what the page calls the group
    backend: str             # the backend SELFIE_BACKEND=diffusers routes it to
    top: int = 16            # all-time most downloaded
    week: int = 12           # most downloaded this week


#: The base models her camera can load, in the order the page shows them. Krea 2
#: fetches deeper because most of its files are formats the backend can't read.
FAMILIES: tuple[Family, ...] = (
    Family("Illustrious", "SDXL · Illustrious", "diffusers"),
    Family("NoobAI", "SDXL · NoobAI", "diffusers"),
    Family("Anima", "Anima", "anima"),
    Family("Krea 2 Turbo", "Krea 2 Turbo", "krea2", top=60),
    Family("Krea 2", "Krea 2", "krea2", top=60),
    Family("Pony", "SDXL · Pony", "diffusers"),
    Family("SDXL 1.0", "SDXL · SDXL 1.0", "diffusers"),
)
_BY_BASE = {f.base_model: f for f in FAMILIES}

# Notes are what the card says under the name: the backend's own fact, plainly.
_NOTES = {
    "Illustrious": ("best", "Illustrious-lineage, the family her camera's defaults and "
                            "prompt wording are tuned for. Loads as-is."),
    "NoobAI": ("best", "Illustrious-derived. Loads as-is."),
    "Pony": ("works", "Loads, but Pony expects score_9 style tags her prompt "
                      "wording doesn't add."),
    "SDXL 1.0": ("works", "Plain SDXL. Loads; her booru-style prompt wording suits "
                          "anime fine-tunes best."),
}
_VPRED = re.compile(r"v-?pred", re.I)
_KREA_INT4 = re.compile(r"int4|convrot", re.I)
_KREA_OTHER = re.compile(r"w4a8|w6a8|nf4|nvfp4|int8|fp8|q\d|gguf", re.I)
_SVDQUANT = re.compile(r"svdquant|nunchaku", re.I)


class CatalogError(RuntimeError):
    """Civitai could not be listed at all."""


# --- what this build would do with a listed checkpoint -------------------------

#: The file types that are a checkpoint's weights. A version also carries its
#: VAE, text encoder and config as files of their own; Krea 2 and Anima exports
#: are usually filed as "Diffusion Model".
WEIGHT_TYPES = ("Model", "Pruned Model", "Diffusion Model")


def _model_files(version: dict) -> list[dict]:
    return [f for f in version.get("files") or []
            if str(f.get("name", "")).endswith(".safetensors")
            and f.get("type", "Model") in WEIGHT_TYPES]


def _primary(files: list[dict]) -> Optional[dict]:
    return next((f for f in files if f.get("primary")), files[0] if files else None)


def _images(version: dict) -> list[dict]:
    out = []
    for image in version.get("images") or []:
        level = int(image.get("nsfwLevel") or 1)
        if image.get("type", "image") != "image" or level >= 32 or not image.get("url"):
            continue
        url = str(image["url"]).replace("/original=true/", f"/width={IMAGE_WIDTH}/")
        out.append({"url": url, "nsfw_level": level})
        if len(out) == 6:
            break
    return out


def classify(model: dict, family: Family) -> Optional[dict]:
    """One listed model as a card: the version and file her camera would use,
    and what this build will do with it. None when no version of it has a file
    this family's backend could even be pointed at."""
    for version in model.get("modelVersions") or []:
        if version.get("baseModel") != family.base_model:
            continue
        files = _model_files(version)
        if family.backend == "krea2":
            pick = next((f for f in files if _KREA_INT4.search(f["name"])
                         and not _KREA_OTHER.search(f["name"])), None)
            if pick is None:
                continue                 # an INT8/FP8/NVFP4/GGUF-only version
            words = f"{model.get('name', '')} {version.get('name', '')} {pick['name']}"
            if _SVDQUANT.search(words):
                status, note = "no", ("SVDQuant (Nunchaku) INT4, a different packing from "
                                      "ConvRot W4A4. The krea2 backend can't read it.")
            elif "convrot" in words.lower() or "dasiwa" in pick["name"].lower():
                status, note = "best", ("INT4 ConvRot, the layout the krea2 backend runs. "
                                        "Needs the .[forge-krea2] extra and access to the "
                                        "gated krea/Krea-2-Raw repo on Hugging Face.")
            else:
                status, note = "likely", ("An INT4 file. The krea2 backend runs only ConvRot "
                                          "W4A4; the layout is checked once it's downloaded.")
        elif family.backend == "anima":
            pick = _primary(files)
            if pick is None:
                return None
            size_gb = (pick.get("sizeKB") or 0) / 1048576
            big = size_gb > 4.5 or re.search(r"2\.9b|3\.8b", f"{version.get('name', '')} "
                                             f"{pick['name']}", re.I)
            status, note = (("no", "Not the 2B Anima. The anima backend builds the 2B model "
                                   "and loads it strictly, so larger variants won't fit.")
                            if big else
                            ("best", "2B Anima. Loads as-is; its text encoder and VAE are "
                                     "fetched once from Hugging Face."))
        else:
            pick = _primary(files)
            if pick is None:
                return None
            if _VPRED.search(f"{version.get('name', '')} {pick['name']}"):
                status, note = "caution", ("V-prediction. The SDXL backend samples it as "
                                           "epsilon, so pictures may come out grey or "
                                           "washed out.")
            else:
                status, note = _NOTES[family.base_model]
        meta = pick.get("metadata") or {}
        stats = model.get("stats") or {}
        return {
            "id": model.get("id"),
            "version_id": version.get("id"),
            "name": model.get("name") or "",
            "version": version.get("name") or "",
            "creator": (model.get("creator") or {}).get("username") or "",
            "downloads": int(stats.get("downloadCount") or 0),
            "family": family.label,
            "base_model": family.base_model,
            "status": status,
            "note": note,
            "file": pick["name"],
            "size_bytes": int((pick.get("sizeKB") or 0) * 1024),
            "precision": meta.get("fp") or "",
            "download_url": pick.get("downloadUrl") or "",
            "nsfw": bool(model.get("nsfw")),
            "published": str(version.get("publishedAt") or "")[:10],
            "url": f"{SITE}/models/{model.get('id')}?modelVersionId={version.get('id')}",
            "images": _images(version),
        }
    return None


def build_catalog(listings: Iterable[tuple[Family, str, list[dict]]]) -> list[dict]:
    """Cards from (family, source, models) listings, in the order given — the
    first listing a version appears in is the one it is filed under."""
    seen: set[tuple[Any, Any]] = set()
    cards = []
    for family, source, models in listings:
        for model in models:
            card = classify(model, family)
            if card is None or (card["id"], card["version_id"]) in seen:
                continue
            seen.add((card["id"], card["version_id"]))
            card["source"] = source
            cards.append(card)
    return cards


async def fetch_catalog(api_key: str = "", *, now: Optional[float] = None) -> dict:
    """List every family from Civitai and classify it. Raises CatalogError when
    nothing could be listed; a family that failed alone is named in `errors`."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    pulls: list[tuple[Family, str, dict]] = []
    for family in FAMILIES:
        base = {"types": "Checkpoint", "baseModels": family.base_model,
                "sort": "Most Downloaded", "nsfw": "true"}
        pulls.append((family, "top", {**base, "limit": family.top}))
        pulls.append((family, "week", {**base, "limit": family.week, "period": "Week"}))
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(client: httpx.AsyncClient, params: dict) -> list[dict]:
        async with gate:
            response = await client.get(f"{API}/models", params=params)
            response.raise_for_status()
            return list(response.json().get("items") or [])

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers=headers,
                                 transport=ASYNC_TRANSPORT) as client:
        results = await asyncio.gather(*(one(client, p) for _, _, p in pulls),
                                       return_exceptions=True)
    listings, errors = [], []
    for (family, source, _), result in zip(pulls, results):
        if isinstance(result, BaseException):
            errors.append(f"{family.base_model} ({source}): {_why(result)}")
        else:
            listings.append((family, source, result))
    if not listings:
        raise CatalogError(errors[0] if errors else "no answer from Civitai")
    return {"fetched_at": time.time() if now is None else now,
            "models": build_catalog(listings), "errors": errors}


def _why(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"Civitai refused the request ({code}): check the API key"
        return f"Civitai answered {code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Civitai didn't answer in time"
    if isinstance(exc, httpx.HTTPError):
        return f"couldn't reach Civitai ({exc.__class__.__name__})"
    return str(exc) or exc.__class__.__name__


class CatalogStore:
    """The last catalog, as one JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Optional[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("models"), list) else None

    def save(self, catalog: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(catalog), encoding="utf-8")
        tmp.replace(self.path)


# --- what is already on this machine -------------------------------------------

PART = ".part"


def safe_name(name: str) -> str:
    """A listed file name as a file in the checkpoint folder, or "" when it
    isn't one: no folders, no hidden files, only .safetensors."""
    name = str(name or "").strip()
    if not name or name != Path(name).name or name.startswith(".") \
            or not name.endswith(".safetensors"):
        return ""
    return name


def scan_folder(folder: Path) -> dict:
    """The checkpoints in `folder` with their verdicts, and any partial
    downloads. Headers only — this is cheap, but still disk: run it off-loop."""
    files, partial = [], {}
    try:
        entries = sorted(Path(folder).iterdir())
    except OSError:
        entries = []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.name.endswith(".safetensors" + PART):
                partial[entry.name[: -len(PART)]] = entry.stat().st_size
            elif entry.name.endswith(".safetensors"):
                verdict = verify_local_checkpoint(entry)
                files.append({"file": entry.name, "size_bytes": entry.stat().st_size,
                              "architecture": verdict.architecture,
                              "status": verdict.status, "note": verdict.note,
                              "title": _title(entry)})
        except OSError:
            continue
    return {"files": files, "partial": partial}


def _title(path: Path) -> str:
    meta = read_safetensors_header(path).get("__metadata__", {}) or {}
    return str(meta.get("modelspec.title") or "")


# --- downloads -----------------------------------------------------------------

@dataclass
class Job:
    version_id: int
    file: str
    folder: Path
    url: str
    total: int = 0
    done: int = 0
    state: str = "queued"     # queued | downloading | done | failed | cancelled
    error: str = ""
    verdict: Optional[dict] = None
    speed: float = 0.0        # bytes/second, smoothed
    cancel: threading.Event = field(default_factory=threading.Event)
    discard: bool = False

    @property
    def active(self) -> bool:
        return self.state in ("queued", "downloading")

    def as_dict(self) -> dict:
        return {"version_id": self.version_id, "file": self.file, "total": self.total,
                "done": self.done, "state": self.state, "error": self.error,
                "verdict": self.verdict, "speed": round(self.speed)}


class Downloads:
    """Every download this process has started, by Civitai version id."""

    def __init__(self) -> None:
        self.jobs: dict[int, Job] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [job.as_dict() for job in self.jobs.values()]

    def get(self, version_id: int) -> Optional[Job]:
        return self.jobs.get(version_id)

    def start(self, card: dict, folder: Path, api_key: str) -> Job:
        """Begin fetching one card's file into `folder`. Raises ValueError for
        a card that can't be fetched and FileExistsError for a file that is
        already there."""
        name = safe_name(card.get("file", ""))
        url = str(card.get("download_url") or "")
        if not name:
            raise ValueError("that listing has no .safetensors file to download")
        if not url.startswith(SITE + "/"):
            raise ValueError("that listing has no Civitai download address")
        if (Path(folder) / name).exists():
            raise FileExistsError(name)
        version_id = int(card["version_id"])
        with self._lock:
            running = self.jobs.get(version_id)
            if running and running.active:
                return running
            job = Job(version_id=version_id, file=name, folder=Path(folder), url=url,
                      total=int(card.get("size_bytes") or 0))
            self.jobs[version_id] = job
        threading.Thread(target=self._run, args=(job, api_key), daemon=True,
                         name=f"civitai-{version_id}").start()
        return job

    def cancel(self, version_id: int) -> bool:
        job = self.jobs.get(version_id)
        if job is None or not job.active:
            return False
        job.discard = True
        job.cancel.set()
        return True

    def stop_all(self) -> None:
        """Server going down: stop every stream, keep every .part to resume."""
        for job in list(self.jobs.values()):
            job.cancel.set()

    # -- the worker thread --

    def _run(self, job: Job, api_key: str) -> None:
        target = job.folder / job.file
        part = job.folder / (job.file + PART)
        try:
            job.folder.mkdir(parents=True, exist_ok=True)
            have = part.stat().st_size if part.exists() else 0
            free = shutil.disk_usage(job.folder).free
            if job.total and job.total - have > free:
                raise _Refused(f"needs {_gb(job.total - have)} free in {job.folder}, "
                               f"and there is {_gb(free)}")
            self._stream(job, part, have, api_key)
            if job.cancel.is_set():
                job.state = "cancelled"
                if job.discard:
                    part.unlink(missing_ok=True)
                return
            part.replace(target)
            verdict = verify_local_checkpoint(target)
            job.verdict = {"architecture": verdict.architecture, "status": verdict.status,
                           "note": verdict.note}
            job.state = "done"
            log.info("civitai: downloaded %s (%s, %s)", job.file, verdict.architecture,
                     verdict.status)
        except _Refused as exc:
            job.state, job.error = "failed", str(exc)
        except (httpx.HTTPError, OSError) as exc:
            job.state, job.error = "failed", _why(exc) if isinstance(exc, httpx.HTTPError) \
                else f"couldn't write the file: {exc.strerror or exc}"
            log.warning("civitai: %s failed: %s", job.file, job.error)

    def _stream(self, job: Job, part: Path, have: int, api_key: str) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if have:
            headers["Range"] = f"bytes={have}-"
        timeout = httpx.Timeout(30.0, read=120.0)
        with httpx.Client(follow_redirects=True, timeout=timeout, transport=TRANSPORT) as client:
            with client.stream("GET", job.url, headers=headers) as response:
                if response.status_code == 416 and have:
                    job.done = job.total = have        # it was already all there
                    return
                if response.status_code in (401, 403):
                    raise _Refused("Civitai refused the download: check your API key, and "
                                   "whether the model asks you to sign in or accept its "
                                   "terms on the site first")
                response.raise_for_status()
                if "text/html" in response.headers.get("content-type", ""):
                    raise _Refused("Civitai sent a web page instead of the file: the API "
                                   "key is probably missing or wrong")
                resumed = response.status_code == 206
                if not resumed:
                    have = 0
                length = int(response.headers.get("content-length") or 0)
                if length:
                    job.total = have + length
                job.done, job.state = have, "downloading"
                mark, mark_done = time.monotonic(), have
                with part.open("ab" if resumed else "wb") as out:
                    for chunk in response.iter_bytes(CHUNK):
                        if job.cancel.is_set():
                            return
                        out.write(chunk)
                        job.done += len(chunk)
                        now = time.monotonic()
                        if now - mark >= 1.0:
                            rate = (job.done - mark_done) / (now - mark)
                            job.speed = rate if not job.speed else 0.7 * job.speed + 0.3 * rate
                            mark, mark_done = now, job.done
        if job.total and job.done < job.total:
            raise _Refused(f"the download stopped at {_gb(job.done)} of {_gb(job.total)}; "
                           f"start it again to resume")


class _Refused(Exception):
    pass


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"
