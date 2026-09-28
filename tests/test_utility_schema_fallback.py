"""Structured output degrades like every other backend (SPEC §2.4).

A route is free not to serve a `json_schema`, and the OpenAI response shape gives
it exactly one way to say so: it answers nothing — `finish_reason: "stop"`, zero
completion tokens. Measured on `openrouter/z-ai/glm-5.2` with the promise-review
schema; the same call in `json_object` mode answers correctly. Read as a parse
failure that surfaced as "promise review was not JSON", which sent every reader
to the parser for an answer the route never sent.

So an empty answer under a schema is retried once without it. These pin that the
retry is narrow enough never to eat a real empty, and that a working schema still
costs exactly one call.
"""
from __future__ import annotations

import pytest

from yurios.app.providers.openrouter import LiteLLMUtilityModel

SCHEMA = {"type": "json_schema",
          "json_schema": {"name": "promise_review", "strict": True,
                          "schema": {"type": "object"}}}


class FakeRoute:
    """litellm.acompletion, answering the way a measured route did.

    `refuses_schema` is glm-5.2's behaviour: empty at `stop`, having written no
    tokens, whenever a json_schema is sent.
    """

    def __init__(self, *, refuses_schema: bool = True, answer: str = '{"goal": null}',
                 finish_reason: str = "stop", reasoning: int = 90,
                 format_costs_thought: bool = False, thoughtful: str = ""):
        self.refuses_schema = refuses_schema
        self.answer = answer
        self.finish_reason = finish_reason
        #: reasoning tokens a call reports; 0 is a model that never reasons
        self.reasoning = reasoning
        #: glm-5.2 on Together: any response_format and the pass is skipped
        self.format_costs_thought = format_costs_thought
        #: what it answers when it did think, if that differs
        self.thoughtful = thoughtful
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        fmt = kwargs.get("response_format") or {}
        silent = fmt.get("type") == "json_schema" and self.refuses_schema
        thought = 0 if (fmt and self.format_costs_thought) else self.reasoning
        content = ("" if silent
                   else self.thoughtful if thought and self.thoughtful
                   else self.answer)
        tokens = 0 if silent else 12

        class Msg:
            pass

        msg = Msg()
        msg.content = content
        choice = type("C", (), {"message": msg, "finish_reason": self.finish_reason})()
        details = type("D", (), {"reasoning_tokens": 0 if silent else thought})()
        usage = type("U", (), {"prompt_tokens": 40, "completion_tokens": tokens,
                               "total_tokens": 40 + tokens,
                               "completion_tokens_details": details})()
        return type("R", (), {"choices": [choice], "usage": usage})()


@pytest.fixture
def route(monkeypatch):
    def install(fake):
        monkeypatch.setattr("litellm.acompletion", fake)
        return fake
    return install


def _model():
    return LiteLLMUtilityModel("openrouter/z-ai/glm-5.2", api_key="k")


async def _complete(model=None, **params):
    model = model or _model()
    return await model.complete_detailed([{"role": "user", "content": "hi"}], **params)


async def test_a_route_that_answers_nothing_under_a_schema_is_asked_again(route):
    fake = route(FakeRoute())
    answer, meta = await _complete(response_format=SCHEMA)

    assert answer == '{"goal": null}'          # the answer, not the silence
    assert meta["schema_downgraded"] is True
    assert len(fake.calls) == 2
    assert fake.calls[0]["response_format"]["type"] == "json_schema"
    assert fake.calls[1]["response_format"] == {"type": "json_object"}
    # the retry is the same call in every other respect
    assert fake.calls[1]["messages"] == fake.calls[0]["messages"]
    assert fake.calls[1]["model"] == fake.calls[0]["model"]


async def test_a_schema_the_route_serves_costs_one_call(route):
    fake = route(FakeRoute(refuses_schema=False))
    answer, meta = await _complete(response_format=SCHEMA)

    assert answer == '{"goal": null}'
    assert "schema_downgraded" not in meta
    assert len(fake.calls) == 1


async def test_an_empty_answer_with_no_schema_is_not_retried(route):
    """Nothing to downgrade to, and nothing to blame the schema for."""
    fake = route(FakeRoute(refuses_schema=False, answer=""))
    answer, meta = await _complete()

    assert answer == ""
    assert "schema_downgraded" not in meta
    assert len(fake.calls) == 1


async def test_a_truncated_answer_is_not_mistaken_for_a_refusal(route):
    """Empty at `length` is the token budget (§2.4), a different bug with a
    different fix — asking again in json_object mode would only burn a call."""
    fake = route(FakeRoute(finish_reason="length"))
    answer, meta = await _complete(response_format=SCHEMA)

    assert answer == ""
    assert "schema_downgraded" not in meta
    assert len(fake.calls) == 1


# ---- a format that costs the thought (SPEC §2.4) ------------------------------
#
# Measured on openrouter/z-ai/glm-5.2: the Together upstream serves any
# response_format with no reasoning pass at all, and answers the promise review
# with a confident, well-formed, wrong {"goal": null}. Asked again without the
# format it reasons and files the goal. Not empty, so the rule above never fires.

LOST = '{"goal": null}'
FOUND = '{"goal": {"text": "read all 24 goal files"}}'


async def test_a_format_that_cost_the_thought_is_dropped_and_asked_again(route):
    fake = route(FakeRoute(refuses_schema=False, format_costs_thought=True,
                           answer=LOST, thoughtful=FOUND))
    model = _model()
    answer, meta = await _complete(model, response_format=SCHEMA)

    assert answer == FOUND                         # the answer it reasoned to
    assert meta["format_dropped"] is True
    assert len(fake.calls) == 2
    assert "response_format" not in fake.calls[1]
    assert fake.calls[1]["messages"] == fake.calls[0]["messages"]
    assert model.reasons is True and model.format_costs_thought is True


async def test_once_learned_the_format_is_not_sent_with_thinking_on(route):
    fake = route(FakeRoute(refuses_schema=False, format_costs_thought=True,
                           answer=LOST, thoughtful=FOUND))
    model = _model()
    await _complete(model, response_format=SCHEMA)
    fake.calls.clear()

    answer, meta = await _complete(model, response_format=SCHEMA)
    assert answer == FOUND and meta["format_dropped"] is True
    assert len(fake.calls) == 1 and "response_format" not in fake.calls[0]

    # …and a call that turned thinking off still gets the format: there is no
    # thought for it to cost.
    fake.calls.clear()
    await _complete(model, response_format=SCHEMA, thinking=False)
    assert fake.calls[0]["response_format"] == SCHEMA


async def test_a_model_that_never_reasons_is_asked_twice_once_and_never_again(route):
    """A plain local model shows no thought with or without a format. The first
    call settles that; after it the format — and its enforcement — stays."""
    fake = route(FakeRoute(refuses_schema=False, reasoning=0))
    model = _model()
    answer, meta = await _complete(model, response_format=SCHEMA)

    assert answer == LOST and "format_dropped" not in meta
    assert len(fake.calls) == 2                    # the one probe
    assert model.reasons is False and model.format_costs_thought is False

    fake.calls.clear()
    await _complete(model, response_format=SCHEMA)
    assert len(fake.calls) == 1
    assert fake.calls[0]["response_format"] == SCHEMA


async def test_a_route_that_reasons_under_the_format_keeps_it(route):
    """Wafer, Mistral: the format and the thought together, in one call."""
    fake = route(FakeRoute(refuses_schema=False))
    model = _model()
    answer, meta = await _complete(model, response_format=SCHEMA)

    assert len(fake.calls) == 1 and "format_dropped" not in meta
    assert model.reasons is True and model.format_costs_thought is False


async def test_a_known_reasoner_that_skips_one_thought_learns_nothing(route):
    """An upstream having a moment, not a format that costs the thought: the
    retry shows no thought either, so the first answer and the format stand."""
    fake = route(FakeRoute(refuses_schema=False, reasoning=0))
    model = _model()
    model.reasons = True
    answer, meta = await _complete(model, response_format=SCHEMA)

    assert answer == LOST and len(fake.calls) == 2
    assert model.reasons is True and model.format_costs_thought is False


async def test_thinking_off_is_never_asked_again_for_its_thought(route):
    fake = route(FakeRoute(refuses_schema=False, reasoning=0))
    await _complete(response_format=SCHEMA, thinking=False)
    assert len(fake.calls) == 1


async def test_an_inline_think_block_is_a_thought(route):
    """A local server that streams reasoning inline reports no count for it."""
    fake = route(FakeRoute(refuses_schema=False, reasoning=0,
                           answer="<think>they only listed them</think>" + FOUND))
    model = _model()
    await _complete(model, response_format=SCHEMA)
    assert len(fake.calls) == 1 and model.reasons is True
