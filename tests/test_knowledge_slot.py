"""The §20.2 knowledge slot — the shelf reaching the context window.

The store, its hybrid scoring and its citations were all built and tested; what
was never built was the wire from `search()` into `assemble()`, so everything
ingested — dropped books, `read_page`, `research` — was indexed and never read.
These pin the wire: the block is there, it carries citations, it stays on the
knowledge side of the §20 boundary, and it is the first thing off the raft.
"""
from __future__ import annotations

from dataclasses import dataclass

from yurios.app.core.assemble import assemble
from yurios.app.core.soul import Soul
from yurios.app.memory.store import Memory


@dataclass
class FakeChunk:
    """Structurally a `mind.knowledge.Chunk` — the app layer takes the shape,
    not the class, because it must not import the mind layer."""
    text: str
    doc: str = "sencha.md"
    span: str = "chars 0-120"

    @property
    def citation(self) -> str:
        return f"{self.doc} ({self.span})"


def soul(**kw) -> Soul:
    base = dict(name="Yuri", card_version="yuri-v1@original",
                voice_law="Speak plainly.", backbone="She is Yuri.",
                personality="warm", scenario="A room with rain on the glass.",
                return_greetings=[], hard_limits="", examples="")
    base.update(kw)
    return Soul(**base)


def build(knowledge=(), memories=(), **kw):
    return assemble(soul(), user_md="Sam plays bass.", summary="",
                    memories=list(memories), lore=[], window=[],
                    user_msg="tell me about sencha", user_name="Sam",
                    knowledge=list(knowledge), **kw)


# ------------------------------------------------------------------- the block

def test_a_shelved_page_reaches_the_system_prompt():
    """The whole point: what she read is in the context window."""
    chunk = FakeChunk("Sencha is steamed rather than pan-fired.")
    prompt = build(knowledge=[chunk])
    assert "WHAT YOU'VE READ" in prompt.system
    assert "Sencha is steamed rather than pan-fired." in prompt.system
    assert prompt.system.index("WHAT YOU'VE READ") < prompt.system.index(
        "THE HONESTY CONSTRAINT"), "the honesty rule must cover the block above it"


def test_the_citation_travels_with_the_text():
    """Groundedness is the reason this store exists: she can be asked to show
    where a claim came from, and can only do that if the span is in the prompt."""
    prompt = build(knowledge=[FakeChunk("Gyokuro is shaded.", doc="tea.md",
                                        span="chars 40-260")])
    assert "tea.md (chars 40-260)" in prompt.system
    assert prompt.citations == ["tea.md (chars 40-260)"]


def test_reading_is_not_remembering():
    """The §20 boundary, held in the prompt as well as on disk: a page she read
    must not arrive dressed as something the user told her."""
    prompt = build(knowledge=[FakeChunk("Bancha is a later flush.")],
                   memories=[Memory(text="Sam plays bass", source="x", kind="turn")])
    read = prompt.system.index("WHAT YOU'VE READ")
    relevant = prompt.system.index("THINGS THAT MAY BE RELEVANT")
    assert relevant < read                      # separate blocks, in §7.1 order
    block = prompt.system[relevant:read]
    assert "Bancha" not in block                # never folded in among memories
    assert "never answer as though they did" in prompt.system


def test_an_empty_shelf_changes_nothing():
    """No shelf, no block — Build #1's prompt, byte for byte."""
    assert build(knowledge=[]).system == build().system
    assert "WHAT YOU'VE READ" not in build().system


# ------------------------------------------------------------------ the budget

def test_the_shelf_cannot_swallow_the_prompt():
    """A chunk is a paragraph budget, so three outweigh every recalled memory
    put together. `search()` returns them ranked, so the tail is the weakest."""
    chunks = [FakeChunk("A" * 1200, doc=f"d{i}.md") for i in range(6)]
    prompt = build(knowledge=chunks, knowledge_budget_tokens=600)
    assert "d0.md" in prompt.system                     # best-scoring kept
    assert "d5.md" not in prompt.system                 # weakest trimmed
    assert len(prompt.citations) < 6


def test_knowledge_leaves_before_she_forgets_you():
    """The overflow ladder (§7.2). Trading away what she remembers about you to
    keep a paragraph of somebody's web page is the wrong trade."""
    mem = Memory(text="Sam's interview was Tuesday", source="x", kind="turn")
    prompt = build(knowledge=[FakeChunk("B" * 3000)], memories=[mem],
                   knowledge_budget_tokens=4000, system_budget_tokens=400)
    assert prompt.dropped_knowledge == 1
    assert prompt.dropped_memories == 0
    assert "Sam's interview was Tuesday" in prompt.system
    assert "WHAT YOU'VE READ" not in prompt.system
