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
