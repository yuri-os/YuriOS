"""`yurios.kernel` is a leaf, and stays one.

The injected clock, the `corr_id` and the `EventHub` began in `yurios/world`
because the server needed them first. Thirteen modules in `yurios/mind` and one
in `yurios/desktop` came to import all three, which made `mind → world` read as
a layering violation in every import block — while actually being the
architecture working: those three *are* AGENTS.md's injected-time, one-bus and
one-`corr_id` rules. The fix was to move them below everything rather than to
stop importing them.

That only stays true while `kernel` depends on nothing above it. The moment one
of these modules grows an import of `world`, `app`, `mind` or `characters`,
every package importing `kernel` is reaching sideways again — and it would be a
one-line change, made for a good local reason, that nothing else would notice.
So: assert the property directly, on the parse tree rather than on a grep, and
let it fail the run instead of rotting quietly.
"""
from __future__ import annotations

import ast
from pathlib import Path

KERNEL = Path(__file__).resolve().parents[1] / "yurios" / "kernel"


def _imported_modules(source: str) -> set[str]:
    """Every module name imported anywhere in the file — nested and function-local
    imports included, because a lazy import is still a dependency."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` inside the package is level>0 with module None;
            # relative imports that climb out (level >= 2) are the ones that leak.
            if node.level >= 2:
                names.add("<relative import above yurios.kernel>")
            elif node.module:
                names.add(node.module)
    return names


def test_the_kernel_imports_nothing_from_yurios():
    offenders: dict[str, set[str]] = {}
    for path in sorted(KERNEL.glob("*.py")):
        leaked = {
            name for name in _imported_modules(path.read_text(encoding="utf-8"))
            if name.startswith("yurios") or name.startswith("<relative")
        }
        if leaked:
            offenders[path.name] = leaked
    assert not offenders, (
        "yurios/kernel must depend on the standard library alone — these reach "
        "back up into the packages that import it:\n" +
        "\n".join(f"  {f}: {', '.join(sorted(n))}" for f, n in sorted(offenders.items())))


def test_the_kernel_still_holds_the_three_primitives():
    """A guard on the guard: the test above passes trivially on an empty package."""
    assert {p.name for p in KERNEL.glob("*.py")} == {
        "__init__.py", "clock.py", "correlate.py", "hub.py"}


# --- import cycles ------------------------------------------------------------

#: The cycles that exist today. Both survive only because at least one side does
#: its import from inside a function, which is what stops Python noticing at
#: import time — and is also what makes them invisible: nothing fails, the
#: coupling just quietly hardens.
#:
#: Like `[tool.mypy]`'s override list, **this may only shrink.** A new entry is
#: not a way to pass the test; it is a decision to add a cycle, and the test
#: refuses to let one arrive unnoticed. `yurios.characters.importer <->
#: yurios.mind.dreamjobs` was the third, removed by moving the dream roster's
#: seed to the module whose prompts it renders.
KNOWN_CYCLES = {
    frozenset({"yurios.app.main", "yurios.app.memory.reindex"}),
    frozenset({"yurios.world.main", "yurios.world.routes.events"}),
}

PKG = Path(__file__).resolve().parents[1] / "yurios"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PKG.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_graph() -> dict[str, set[str]]:
    """module -> the yurios modules it imports, function-local imports included."""
    modules = {_module_name(p): p for p in PKG.rglob("*.py")
               if "__pycache__" not in str(p)}
    graph: dict[str, set[str]] = {m: set() for m in modules}

    def add(me: str, target: str) -> None:
        # Resolve to the nearest module that actually exists: `yurios.mind.loop.X`
        # is an import OF `yurios.mind.loop`.
        while target and target not in modules:
            target = target.rpartition(".")[0]
        if target and target != me:
            graph[me].add(target)

    for me, path in modules.items():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("yurios"):
                        add(me, a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = me.split(".")
                    base = base[:-node.level] if node.level <= len(base) else []
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                if not target.startswith("yurios"):
                    continue
                # `from pkg import submodule` is an edge to the SUBMODULE. Missing
                # this is how a cycle hides: it reads as an edge to the package.
                for a in node.names:
                    if f"{target}.{a.name}" in modules:
                        add(me, f"{target}.{a.name}")
                add(me, target)
    return graph


def _cycles(graph: dict[str, set[str]]) -> set[frozenset[str]]:
    """Every strongly-connected component of more than one module (Tarjan,
    iterative — the graph is small but recursion depth is not worth betting on)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: set[frozenset[str]] = set()
    counter = 0

    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph[child]))))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1:
                    found.add(frozenset(component))
    return found


def test_no_import_cycle_arrives_unnoticed():
    cycles = _cycles(_import_graph())
    new = cycles - KNOWN_CYCLES
    assert not new, (
        "new import cycle(s). A function-local import makes one work at runtime "
        "and does not make it not a cycle:\n" +
        "\n".join("  " + " <-> ".join(sorted(c)) for c in sorted(map(sorted, new))))


def test_a_fixed_cycle_leaves_the_list():
    """KNOWN_CYCLES may only shrink, so a cycle that has been broken has to be
    struck off — otherwise the list stops describing the codebase and starts
    being a place things go to be forgotten."""
    gone = KNOWN_CYCLES - _cycles(_import_graph())
    assert not gone, (
        "these no longer cycle — delete them from KNOWN_CYCLES:\n" +
        "\n".join("  " + " <-> ".join(sorted(c)) for c in sorted(map(sorted, gone))))
