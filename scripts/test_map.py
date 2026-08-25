#!/usr/bin/env python
"""Regenerate `docs/test-map.md` — module → the test files that exercise it.

The naming convention gets you most of the way: `world/inbox.py` is covered by
`tests/test_inbox.py`, and for 40-odd modules that is the whole answer. For the
rest it is silently untrue. `world/brain.py`, `world/main.py`, `cli.py` and
`migrate.py` have no `test_<name>.py` at all and are covered thoroughly — by
`test_integration.py`, `test_host.py` and friends. An agent editing one of them
cannot *derive* what to run, so it either runs everything or guesses.

So: measure it. Coverage with `dynamic_context = "test_function"` records which
test was executing when each line ran, which turns "what covers this module"
into a database query rather than a convention nobody can check.

    python scripts/test_map.py            # run the suite under coverage, rewrite the map
    python scripts/test_map.py --check    # fail if the committed map is stale
    python scripts/test_map.py --reuse    # rebuild from an existing .coverage, no rerun

Deliberately NOT part of `./scripts/check.sh`. An instrumented run is minutes,
not seconds, and the gate's whole value is that it is cheap enough to run every
time. This is a thing you regenerate when module boundaries move.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "test-map.md"
DATA = ROOT / ".coverage"

#: Below this share of a module's covered lines, a test file is noise — it
#: touched the module in passing (an import, a constructor) rather than
#: exercising it. Tuned so the map answers "what do I run" and not "what
#: happens to load this".
MIN_SHARE = 0.10
#: …and never list more than this many per module: the point is a short answer.
MAX_TESTS = 6


def run_suite() -> None:
    """The suite under coverage, in parallel, then combined.

    Two things this deliberately does not do, both of which make the map come
    back empty rather than wrong — which is worse, because empty looks like a
    result:

      * `COVERAGE_CORE=sysmon`. Python 3.12's `sys.monitoring` backend is much
        the faster one and does not record dynamic contexts; every context
        comes back as the empty string.
      * `-n 8`. xdist runs the tests in execnet subprocesses that `coverage
        run` never wrapped, so the controller registers every module it
        imported during collection and reports zero lines for all of them.

    So this is the serial suite under the C tracer. It is the slow way on
    purpose, and it is why this is a script and not a gate stage.
    """
    print("==> running the suite under coverage (serial, C tracer; a few minutes)")
    subprocess.run(
        [sys.executable, "-m", "coverage", "run",
         "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, check=False)


def collect() -> tuple[dict[str, collections.Counter], set[str]]:
    """(module → Counter(test file → lines covered), modules run only at import).

    The second half matters more than it looks. A dynamic context is recorded
    only while a test function is on the stack, so a module whose lines are all
    *declarations* — a Protocol, a pydantic Config, an ABC — executes entirely
    at import time and lands in no context at all. Reporting those as "no test
    covers this" would be false, and falsely alarming: `world/config.py` is
    exercised by most of the suite.
    """
    from coverage import CoverageData

    data = CoverageData(basename=str(DATA))
    data.read()
    out: dict[str, collections.Counter] = {}
    import_only: set[str] = set()
    for measured in data.measured_files():
        path = pathlib.Path(measured)
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            continue
        if rel.parts[0] != "yurios" or rel.suffix != ".py":
            continue
        counts: collections.Counter = collections.Counter()
        for _line, contexts in (data.contexts_by_lineno(measured) or {}).items():
            for context in contexts:
                test_file = _context_file(context)
                if test_file:
                    counts[test_file] += 1
        if counts:
            out[str(rel)] = counts
        elif data.lines(measured):
            import_only.add(str(rel))
    return out, import_only


def _context_file(context: str) -> str | None:
    """`tests.test_host.test_x` → `tests/test_host.py`.

    Coverage writes the context as a dotted path, not as pytest's `file::name`
    nodeid, and a test inside a class adds a component — so the file is the
    longest prefix that is actually a file, found by asking the filesystem
    rather than by counting dots.
    """
    parts = context.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = pathlib.Path(*parts[:cut]).with_suffix(".py")
        if (ROOT / candidate).is_file():
            return candidate.as_posix()
    return None


def render(covered: dict[str, collections.Counter],
           import_only: set[str]) -> str:
    modules = sorted(
        p for p in (ROOT / "yurios").rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in str(p))
    rows = []
    uncovered = []
    declarations = []
    for path in modules:
        rel = str(path.relative_to(ROOT))
        counts = covered.get(rel)
        if not counts:
            (declarations if rel in import_only else uncovered).append(rel)
            continue
        # Sorted by (-lines, name), never `most_common`: ties there fall back
        # to insertion order, which is whatever order coverage happened to
        # yield contexts in, so two renderings of the same database disagree
        # and `--check` fails on a file nobody touched.
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top = ranked[0][1]
        best = [(name, n) for name, n in ranked[:MAX_TESTS]
                if n >= top * MIN_SHARE]
        derivable = f"tests/test_{path.stem}.py"
        marks = [f"**`{n}`**" if n == derivable else f"`{n}`" for n, _ in best]
        rows.append((rel, "<br>".join(marks), len(counts)))

    lines = [
        "# Test map",
        "",
        "Which test files actually exercise each module — **generated**, not written:",
        "`python scripts/test_map.py`. Do not hand-edit.",
        "",
        "Measured with `coverage`'s per-test contexts, so this is what *ran*, not what",
        "the file names suggest. A test file appears here if it covered at least",
        f"{int(MIN_SHARE * 100)}% as many lines of the module as its top coverer did;",
        f"at most {MAX_TESTS} are listed, best first. **Bold** marks the one the naming",
        "convention would have guessed — where it is absent, the convention is lying,",
        "and that is the case this file exists for.",
        "",
        "| Module | Run these | Files touching it |",
        "|---|---|---|",
    ]
    lines += [f"| `{rel}` | {tests} | {n} |" for rel, tests, n in rows]
    if declarations:
        lines += [
            "",
            f"## Declarations only ({len(declarations)})",
            "",
            "Every line ran at import, none inside a test — Protocols, pydantic Config",
            "schemas, ABCs. These are exercised by much of the suite; there is simply no",
            "*statement* of theirs for a test to be executing when it happens.",
            "",
        ]
        lines += [f"- `{rel}`" for rel in declarations]
    if uncovered:
        lines += [
            "",
            f"## Never executed ({len(uncovered)})",
            "",
            "Not one line, not even at import. Some are legitimately unreachable offline",
            "(a GPU backend, a native window, a `__main__`); the rest are a list worth",
            "shortening.",
            "",
        ]
        lines += [f"- `{rel}`" for rel in uncovered]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if docs/test-map.md is out of date")
    ap.add_argument("--reuse", action="store_true",
                    help="rebuild from an existing .coverage without rerunning")
    args = ap.parse_args()

    if not args.reuse and not args.check:
        run_suite()
    if not DATA.exists():
        print("no .coverage data — run without --reuse/--check first", file=sys.stderr)
        return 2
    text = render(*collect())
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is out of date — run: "
                  "python scripts/test_map.py", file=sys.stderr)
            return 1
        print(f"{OUT.relative_to(ROOT)} is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
