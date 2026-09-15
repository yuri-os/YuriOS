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
                 finish_reason: str = "stop"):
        self.refuses_schema = refuses_schema
        self.answer = answer
        self.finish_reason = finish_reason
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        schema = (kwargs.get("response_format") or {}).get("type") == "json_schema"
        silent = schema and self.refuses_schema
        content = "" if silent else self.answer
        tokens = 0 if silent else 12

        class Msg:
            pass

        msg = Msg()
        msg.content = content
        choice = type("C", (), {"message": msg, "finish_reason": self.finish_reason})()
        usage = type("U", (), {"prompt_tokens": 40, "completion_tokens": tokens,
                               "total_tokens": 40 + tokens,
                               "completion_tokens_details": None})()
        return type("R", (), {"choices": [choice], "usage": usage})()


@pytest.fixture
def route(monkeypatch):
    def install(fake):
        monkeypatch.setattr("litellm.acompletion", fake)
        return fake
    return install


async def _complete(**params):
    model = LiteLLMUtilityModel("openrouter/z-ai/glm-5.2", api_key="k")
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
