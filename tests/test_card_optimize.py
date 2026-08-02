"""Optimize with AI: the merge, the salvage, and the refusal to write anything.

There is no model in these tests. What is being pinned is everything *around*
the model — because the model is the one part that will be different on every
machine, and the parts that have to hold regardless are: a hostile or sloppy
answer cannot put anything unexpected into a draft, a cut-off answer is worth
keeping, and pressing optimize never touches the Vault.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from tests.support.cards import card_data, png_card, wrapper
from yurios.characters import CharacterImporter, CharacterRegistry
from yurios.characters.optimize import (
    PASSES, CardOptimizeError, Optimization, build_messages, card_material,
    changes, merge, optimize_draft, token_budget,
)
from yurios.characters.studio import Draft, read_draft
from yurios.world.config import Config
from yurios.world.host import create_host_app


@pytest.fixture
def node(tmp_path):
    cfg = Config(_env_file=None, data_dir=tmp_path / "data",
                 telegram_bot_token="", telegram_chat_id="")
    registry = CharacterRegistry(tmp_path / "data")
    CharacterImporter(registry, initialize_git=True).import_card(
        png_card(wrapper(card_data(), native=True)), character_id="subject")
    with TestClient(create_host_app(cfg, registry)) as client:
        yield client, registry


def sample() -> Draft:
    return Draft(name="Virelle", identity="Everything, in one blob.",
                 first_mes="Oh — it's you.", tags=["academy"])


class FakeUtility:
    """A utility model that answers with whatever the test hands it.

    `answers` may be one string (every pass gets it) or one per pass. The call
    log is what pins the pass structure: three calls, each carrying only its own
    group's material.
    """

    def __init__(self, *answers: str, usage: dict | None = None):
        self.answers = list(answers) or [""]
        self.usage = usage or {"finish_reason": "stop", "prompt_tokens": 100,
                               "completion_tokens": 50, "total_tokens": 150,
                               "reasoning_tokens": 0}
        self.calls: list[dict] = []

    def _next(self, messages, params) -> str:
        self.calls.append({"messages": messages, **params})
        index = min(len(self.calls) - 1, len(self.answers) - 1)
        return self.answers[index]

    async def complete_detailed(self, messages, **params):
        return self._next(messages, params), dict(self.usage)

    async def complete(self, messages, **params):
        return self._next(messages, params)


class OldUtility(FakeUtility):
    """A provider from before `complete_detailed` — the fallback path."""

    complete_detailed = None


# ---------------------------------------------------------------- the merge

def test_only_known_fields_survive_the_merge():
    draft = sample()
    result = merge(draft, {"fields": {
        "manner": "Dry, and watching.",
        "description": "should be ignored — it is derived",
        "paths": {"vault": "/etc"},
        "__class__": "nope",
    }})

    assert result.manner == "Dry, and watching."
    assert result.identity == draft.identity
    assert not hasattr(result, "paths")
    # `description` is derived from the backbone, so the model's attempt to set
    # it is not merely ignored — it cannot be represented at all.
    assert result.description == f"{result.identity}\n\n{result.manner}"
    assert "derived" not in result.description


def test_a_field_of_the_wrong_type_keeps_what_it_had():
    draft = sample()
    result = merge(draft, {"fields": {"identity": ["a", "list"],
                                      "tags": {"not": "a list"},
                                      "manner": 5}})

    assert result.identity == draft.identity
    assert result.tags == ["academy"]
    assert result.manner == "5"                    # a scalar is a fine string


def test_an_omitted_or_null_field_is_left_alone():
    result = merge(sample(), {"fields": {"manner": None, "history": "Ravenholds."}})

    assert result.first_mes == "Oh — it's you."
    assert result.history == "Ravenholds."


def test_the_name_is_never_blanked():
    """No instruction is worth a card that cannot be imported anywhere."""
    result = merge(sample(), {"fields": {"name": "   "}})

    assert result.name == "Virelle"


def test_a_model_that_skipped_the_envelope_is_still_understood():
    result = merge(sample(), {"manner": "Dry."})

    assert result.manner == "Dry."


def test_a_lorebook_entry_with_no_keys_is_dropped():
    result = merge(sample(), {"fields": {"lorebook": {"entries": [
        {"name": "Valtheria", "keys": ["continent"], "content": "A land."},
        {"name": "Nothing", "keys": [], "content": "unreachable"},
        {"name": "Empty", "keys": ["x"], "content": ""},
    ]}}})

    assert [entry["name"] for entry in result.lorebook["entries"]] == ["Valtheria"]
    assert result.lorebook["scan_depth"] == 4       # the untouched half survives


def test_the_diff_names_what_was_filled_and_what_was_emptied():
    before = Draft(name="V", identity="who", manner="dry")
    after = merge(before, {"fields": {"manner": "", "history": "Ravenholds."}})
    diff = {item["field"]: item for item in changes(before, after)}

    assert diff["history"]["filled"] and not diff["history"]["emptied"]
    assert diff["manner"]["emptied"] and not diff["manner"]["filled"]
    assert diff["manner"]["before"] == "dry"
    assert "identity" not in diff


# ---------------------------------------------- what the model is handed/gives

def test_the_material_carries_the_fields_and_not_the_empties():
    payload = json.loads(card_material(sample()))

    assert payload["identity"] == "Everything, in one blob."
    assert "manner" not in payload and "description" not in payload


def test_the_user_instruction_reaches_the_system_prompt():
    messages = build_messages(sample(), "make her devoted")

    assert "make her devoted" in messages[0]["content"]
    assert "identity" in messages[1]["content"]      # the material
    assert "manner" in messages[1]["content"]        # named as a fillable hole


def test_a_fenced_answer_behind_a_reasoning_block_is_read():
    """Two things every local reasoning model does, at once."""
    answer = ("<think>I should move the backstory.</think>\n"
              '```json\n{"notes": "moved it", "fields": {"history": "Ravenholds."}}\n```')
    result = _optimize(sample(), answer)

    assert result.draft.history == "Ravenholds."
    assert result.notes == "moved it"
    assert not result.truncated


def test_braces_inside_her_prose_do_not_close_the_object_early():
    answer = ('{"notes": "n", "fields": {"first_mes": "Hello {{user}}. }} still me.",'
              ' "manner": "Dry."}}')
    result = _optimize(sample(), answer)

    assert result.draft.first_mes == "Hello {{user}}. }} still me."
    assert result.draft.manner == "Dry."


def test_an_answer_cut_off_mid_field_keeps_the_fields_it_finished():
    answer = ('{"notes": "n", "fields": {"history": "Ravenholds.", '
              '"manner": "She notices everything and')
    result = _optimize(sample(), answer)

    assert result.draft.history == "Ravenholds."
    assert result.draft.manner == ""                 # never half-written
    assert result.truncated


def test_an_answer_cut_off_between_fields_is_salvaged_too():
    answer = ('{"notes": "n", "fields": {"history": "Ravenholds.", "manner":')
    result = _optimize(sample(), answer)

    assert result.draft.history == "Ravenholds."
    assert result.truncated


def test_an_unsalvageable_answer_says_so_in_words():
    with pytest.raises(CardOptimizeError, match="valid JSON"):
        _optimize(sample(), "I'm sorry, I can't help with that request.")


def test_an_answer_that_changes_nothing_is_a_refusal_not_a_result():
    with pytest.raises(CardOptimizeError, match="no changes"):
        _optimize(sample(), '{"notes": "already fine", "fields": {}}')


def test_no_model_at_all_is_a_sentence_the_user_can_act_on():
    import asyncio
    with pytest.raises(CardOptimizeError, match="pick one"):
        asyncio.run(optimize_draft(None, sample()))


def test_the_budget_leaves_room_to_think_before_it_answers():
    """The whole feature turns on this number. A budget sized to the answer is
    one a reasoning model spends entirely on reasoning."""
    from yurios.characters.optimize import REASONING_ALLOWANCE

    small = token_budget(Draft(name="A", identity="x"))
    large = token_budget(Draft(name="A", identity="x" * 40_000))

    assert small > REASONING_ALLOWANCE
    assert small < large <= 32768


# ---------------------------------------------------------------- the passes

def test_the_card_is_re_filed_in_passes_each_carrying_only_its_own_material():
    """The whole reason this is not one call: a reasoning model spends its
    think budget out of the answer's window, so each ask has to be small."""
    draft = Draft(name="V", identity="who she is", scenario="a lore dump",
                  creator_notes="by someone")
    model = FakeUtility('{"fields": {"manner": "Dry."}}')
    import asyncio
    asyncio.run(optimize_draft(model, draft))

    assert len(model.calls) == len(PASSES)
    persona, scene, frame = (call["messages"][1]["content"] for call in model.calls)
    assert "a lore dump" not in persona          # the persona pass never sees it
    assert "a lore dump" in scene
    assert "by someone" in frame and "by someone" not in persona
    # each pass is told about its own fields only
    assert "- lorebook:" in model.calls[1]["messages"][0]["content"]
    assert "- lorebook:" not in model.calls[0]["messages"][0]["content"]
    assert "- appearance:" in model.calls[0]["messages"][0]["content"]


def test_a_pass_may_only_write_its_own_fields():
    """A model that answers the next pass's question early must not have that
    answer silently overwritten two calls later."""
    draft = Draft(name="V", identity="who")
    result = _optimize(draft, '{"fields": {"manner": "Dry.", "creator": "someone"}}')

    assert result.draft.manner == "Dry."         # persona's to set
    assert result.draft.creator == "someone"     # frame's — landed on its own pass
    assert [item["field"] for item in result.changes] == ["creator", "manner"]


def test_a_pass_that_fails_does_not_lose_the_passes_that_worked():
    result = _optimize(Draft(name="V", identity="who"),
                       '{"fields": {"manner": "Dry."}}',
                       "sorry, I can't do that",
                       "also not json")

    assert result.draft.manner == "Dry."
    assert len(result.failed) == 2
    assert "where she is" in result.failed[0]


def test_an_empty_answer_is_retried_with_a_shorter_reasoning_pass():
    """The observed local-model failure: it thinks until the window is gone and
    answers with nothing. Thinking stays on; it is asked to think less."""
    model = FakeUtility("<think>still working on it</think>",
                        '{"fields": {"manner": "Dry."}}',
                        '{"fields": {}}', '{"fields": {}}')
    import asyncio
    result = asyncio.run(optimize_draft(model, Draft(name="V", identity="who")))

    assert result.draft.manner == "Dry."
    assert model.calls[0].get("reasoning_effort") is None
    assert model.calls[1]["reasoning_effort"] == "low"


def test_a_model_that_thinks_itself_out_of_room_says_so_in_numbers():
    spent = {"finish_reason": "length", "prompt_tokens": 5319,
             "completion_tokens": 2873, "total_tokens": 8192,
             "reasoning_tokens": 2870}
    import asyncio
    with pytest.raises(CardOptimizeError) as raised:
        asyncio.run(optimize_draft(
            FakeUtility("<think>thinking</think>", usage=spent),
            Draft(name="V", identity="who")))

    message = str(raised.value)
    assert "8192" in message and "5319" in message and "2870" in message
    assert "context" in message


def test_a_provider_without_the_detailed_call_still_works():
    """`complete_detailed` is an addition to the seam, not a requirement of it."""
    import asyncio
    result = asyncio.run(optimize_draft(
        OldUtility('{"fields": {"manner": "Dry."}}'), Draft(name="V", identity="who")))

    assert result.draft.manner == "Dry."


def _optimize(draft: Draft, *answers: str, **kwargs) -> Optimization:
    """Run the real runner over a scripted model. One answer means every pass
    gets it; `steps=` narrows the run to the pass under test."""
    import asyncio
    return asyncio.run(optimize_draft(FakeUtility(*answers), draft,
                                      model="fake/m", **kwargs))


# -------------------------------------------------------------------- routes

def test_the_route_proposes_and_writes_nothing(node, monkeypatch):
    client, registry = node
    record = registry.get("subject")
    before = (record.paths.vault / "soul" / "PERSONA.md").read_text()

    monkeypatch.setattr(
        "yurios.app.providers.openrouter.LiteLLMUtilityModel.complete_detailed",
        FakeUtility('{"notes": "moved", "fields": {"manner": "Dry, and watching."}}',
                    '{"fields": {}}', '{"fields": {}}').complete_detailed)
    draft = client.get("/api/characters/subject/studio").json()["draft"]
    response = client.post("/api/studio/optimize",
                           json={"draft": draft, "character": "subject",
                                 "instructions": "warmer", "model": "fake/model"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"]["manner"] == "Dry, and watching."
    assert [item["field"] for item in payload["changes"]] == ["manner"]
    assert payload["model"] == "fake/model"
    # the whole point: a proposal is not a save
    assert (record.paths.vault / "soul" / "PERSONA.md").read_text() == before


def test_a_model_failure_is_a_502_with_something_to_read(node, monkeypatch):
    client, _ = node

    async def explode(self, messages, **params):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(
        "yurios.app.providers.openrouter.LiteLLMUtilityModel.complete_detailed",
        explode)
    response = client.post("/api/studio/optimize",
                           json={"draft": {"name": "V", "identity": "who"}})

    assert response.status_code == 502
    assert "connection refused" in response.json()["detail"]


def test_the_optimizer_can_be_used_before_a_character_exists(node, monkeypatch):
    """The studio's create mode has no character id, and the dialog still works."""
    client, _ = node
    monkeypatch.setattr(
        "yurios.app.providers.openrouter.LiteLLMUtilityModel.complete_detailed",
        FakeUtility('{"fields": {"appearance": "Pale, with black wings."}}',
                    '{"fields": {}}', '{"fields": {}}').complete_detailed)

    response = client.post("/api/studio/optimize",
                           json={"draft": {"name": "V", "identity": "who"}})

    assert response.status_code == 200
    assert response.json()["draft"]["appearance"] == "Pale, with black wings."


def test_the_model_list_answers_for_a_character_under_review(node):
    client, _ = node
    payload = client.get("/api/studio/models?provider=nonsense").json()

    assert payload["models"] == []
    assert "no live listing" in payload["error"]


def test_an_imported_card_arrives_already_sectioned(node):
    """The deterministic router runs at import, so the studio opens on four
    sections rather than one blob — with or without a model in reach."""
    client, registry = node
    CharacterImporter(registry, initialize_git=False).import_card(
        png_card(card_data(
            description="Name: Kes\n\nAppearance: tall, grey-eyed.\n\n"
                        "Backstory: raised by the river.",
            personality="")),
        character_id="kes")
    draft, _provenance = read_draft(registry.get("kes"))

    assert "grey-eyed" in draft.appearance
    assert "river" in draft.history
    assert "grey-eyed" not in draft.identity


# ------------------------------------------------------------------ progress

def test_every_pass_announces_itself_starting_and_finishing():
    """Three calls to a reasoning model is minutes of nothing to look at. What
    makes that bearable is knowing which ask is outstanding, so each pass says
    when it starts and what it moved when it stops."""
    import asyncio
    seen: list[dict] = []
    asyncio.run(optimize_draft(
        FakeUtility('{"fields": {"manner": "Dry."}}',
                    '{"fields": {"scenario": "A rainlit library."}}',
                    '{"fields": {}}'),
        Draft(name="V", identity="who"), on_progress=seen.append))

    starts = [event for event in seen if event["state"] == "start"]
    assert [event["index"] for event in starts] == [1, 2, 3]
    assert {event["total"] for event in starts} == {3}
    assert [event["label"] for event in starts] == [p.label for p in PASSES]
    done = {event["index"]: event for event in seen if event["state"] == "done"}
    assert done[1]["fields"] == ["manner"]
    assert done[2]["fields"] == ["scenario"]
    assert done[3]["fields"] == []          # a pass that moved nothing still reports


def test_a_pass_that_fails_says_so_on_the_way_past():
    import asyncio
    seen: list[dict] = []
    asyncio.run(optimize_draft(
        FakeUtility("", "", '{"fields": {"scenario": "A rainlit library."}}'),
        Draft(name="V", identity="who"), on_progress=seen.append))

    failed = [event for event in seen if event["state"] == "failed"]
    assert failed and failed[0]["index"] == 1
    assert "thought" in failed[0]["message"] or "answer" in failed[0]["message"]


def test_the_retry_is_announced_because_it_doubles_the_wait():
    """An empty first attempt buys a second one with more room. A clock that
    then runs twice as long with no explanation is what makes people close the
    tab, so the retry is named where they can see it."""
    import asyncio
    seen: list[dict] = []
    asyncio.run(optimize_draft(
        FakeUtility("", '{"fields": {"manner": "Dry."}}'),
        Draft(name="V", identity="who"), on_progress=seen.append,
        steps=PASSES[:1]))

    assert [event["state"] for event in seen] == ["start", "retry", "done"]


def test_a_listener_that_raises_cannot_fail_the_run():
    """The listener is a browser that may have closed. It is decoration."""
    import asyncio

    def hostile(event):
        raise RuntimeError("the tab is gone")

    result = asyncio.run(optimize_draft(
        FakeUtility('{"fields": {"manner": "Dry."}}'), Draft(name="V", identity="who"),
        on_progress=hostile))

    assert result.draft.manner == "Dry."


def test_the_route_streams_a_line_per_pass_when_asked_to(node, monkeypatch):
    client, _ = node
    monkeypatch.setattr(
        "yurios.app.providers.openrouter.LiteLLMUtilityModel.complete_detailed",
        FakeUtility('{"fields": {"manner": "Dry, and watching."}}',
                    '{"fields": {}}', '{"fields": {}}').complete_detailed)

    response = client.post("/api/studio/optimize",
                           headers={"Accept": "application/x-ndjson"},
                           json={"draft": {"name": "V", "identity": "who"}})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert [event["event"] for event in events][-1] == "done"
    assert sum(1 for event in events
               if event["event"] == "pass" and event["state"] == "start") == 3
    assert events[-1]["result"]["draft"]["manner"] == "Dry, and watching."


def test_a_streamed_failure_is_the_last_line_not_a_status_code(node, monkeypatch):
    """The status went out before the first pass ran, so 200 is all it can be.
    The failure still has to arrive, and with the same sentence the JSON route
    would have put in `detail`."""
    client, _ = node

    async def explode(self, messages, **params):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(
        "yurios.app.providers.openrouter.LiteLLMUtilityModel.complete_detailed",
        explode)
    response = client.post("/api/studio/optimize",
                           headers={"Accept": "application/x-ndjson"},
                           json={"draft": {"name": "V", "identity": "who"}})

    assert response.status_code == 200
    last = json.loads(response.text.splitlines()[-1])
    assert last["event"] == "error"
    assert "connection refused" in last["message"]


def test_a_client_that_does_not_ask_for_the_stream_still_gets_one_object(node, monkeypatch):
    """A script calling this endpoint should not have to learn a protocol."""
    client, _ = node
    monkeypatch.setattr(
        "yurios.app.providers.openrouter.LiteLLMUtilityModel.complete_detailed",
        FakeUtility('{"fields": {"manner": "Dry."}}',
                    '{"fields": {}}', '{"fields": {}}').complete_detailed)

    response = client.post("/api/studio/optimize",
                           json={"draft": {"name": "V", "identity": "who"}})

    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["draft"]["manner"] == "Dry."


# ------------------------------------------------------------------ examples

def test_the_frame_pass_is_handed_her_voice_so_it_can_write_in_it():
    """`examples` is the one field the optimiser may compose, and composing an
    exchange without her manner in front of you produces dialogue in nobody's
    voice. The pass that writes them therefore carries her voice as material,
    even though it does not write any of those fields."""
    frame = PASSES[-1]
    assert "examples" in frame.produce
    for field in ("manner", "scenario", "first_mes"):
        assert field in frame.material
        assert field not in frame.produce      # material only — it may not edit them

    draft = Draft(name="V", identity="who", manner="Dry, and watching.",
                  first_mes="*She does not look up.* You're late.")
    material = json.loads(card_material(draft, frame.material))
    assert material["manner"] == "Dry, and watching."
    assert material["first_mes"].endswith("You're late.")


def test_composing_examples_is_named_as_the_one_exception_to_inventing():
    """The brief says 'do not invent facts' and then asks for written exchanges.
    A model reading both has to be told which one wins, or it will decline."""
    prompt = build_messages(Draft(name="V", identity="who"), "", PASSES[-1])[0]["content"]

    assert "Do NOT invent facts about her" in prompt
    assert "one exception" in prompt
    assert "may compose exchanges when the card has none" in prompt
    assert "an invented fact" in prompt


def test_the_authors_own_examples_are_never_replaced_by_written_ones():
    """A card that ships examples keeps them. The model is told to keep them,
    and if it ignores that, the merge is still the author's call to make — so
    this pins the instruction rather than a guardrail that does not exist."""
    prompt = build_messages(
        Draft(name="V", identity="who", examples=["{{user}}: Hi\n{{char}}: Hm."]),
        "", PASSES[-1])
    guide = prompt[0]["content"]

    assert "If the card has them, keep them" in guide
    assert "verbatim" in guide
    assert "{{user}}: Hi" in prompt[1]["content"]    # handed back to it as material
