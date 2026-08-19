"""Showing her a picture (SPEC §35) — the capability probe, the upload shelf,
and the whole path from a POSTed file to the image part a model reads.

Offline like everything else: the providers answer over `httpx.MockTransport`,
the shelf works on PNGs Pillow makes here, and the route path runs on FakeBrain,
which records the data url instead of looking at it.
"""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from yurios.app.core import assemble as asm
from yurios.app.providers.vision import probe
from yurios.world.context import IMAGE_TOKENS, estimate_messages
from yurios.world.uploads import UploadRejected, Uploads

pytest.importorskip("fastapi")
from starlette.testclient import TestClient                   # noqa: E402

from yurios.desktop.voice.backends.fakes import FakeBrain     # noqa: E402
from yurios.world.main import create_app                      # noqa: E402


def png(width: int = 40, height: int = 30, colour=(20, 140, 90), mode="RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---- the probe: does this model take images? (app/providers/vision.py) ------

async def test_lmstudio_capabilities_are_believed(cfg):
    cfg = cfg.model_copy(update={"chat_model": "lm_studio/qwen/qwen3-vl",
                                 "chat_image_input": "auto"})

    def handler(request):
        assert request.url.path == "/api/v1/models"
        return httpx.Response(200, json={"models": [
            {"key": "qwen/qwen3-vl", "capabilities": {"vision": True}},
            {"key": "other/text-only", "capabilities": {"vision": False}}]})

    can, why = await probe(cfg, transport=transport(handler))
    assert can and "LM Studio" in why


async def test_lmstudio_text_only_model_is_taken_at_its_word(cfg):
    cfg = cfg.model_copy(update={"chat_model": "lm_studio/other/text-only",
                                 "chat_image_input": "auto"})
    handler = lambda r: httpx.Response(200, json={"models": [           # noqa: E731
        {"key": "other/text-only", "capabilities": {"trained_for_tool_use": True}}]})

    can, _ = await probe(cfg, transport=transport(handler))
    assert can is False


async def test_an_older_lmstudio_answers_on_the_v0_listing(cfg):
    """LM Studio before the v1 developer API says the same thing by typing the
    model `vlm` — a house that hasn't updated still gets its paperclip."""
    cfg = cfg.model_copy(update={"chat_model": "lm_studio/qwen/qwen3-vl",
                                 "chat_image_input": "auto"})

    def handler(request):
        if request.url.path == "/api/v1/models":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [
            {"id": "qwen/qwen3-vl", "type": "vlm"}]})

    can, _ = await probe(cfg, transport=transport(handler))
    assert can is True


async def test_ollama_capabilities_are_asked_for_by_model(cfg):
    cfg = cfg.model_copy(update={"chat_model": "ollama/llama3.2-vision",
                                 "chat_image_input": "auto"})
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"capabilities": ["completion", "vision"]})

    can, why = await probe(cfg, transport=transport(handler))
    assert can and "Ollama" in why
    assert seen == {"model": "llama3.2-vision"}


async def test_openrouter_input_modalities_decide(cfg):
    cfg = cfg.model_copy(update={"chat_model": "openrouter/qwen/qwen3-vl",
                                 "chat_image_input": "auto"})
    handler = lambda r: httpx.Response(200, json={"data": [               # noqa: E731
        {"id": "qwen/qwen3-vl",
         "architecture": {"input_modalities": ["text", "image", "video"]}},
        {"id": "z-ai/glm", "architecture": {"input_modalities": ["text"]}}]})

    can, _ = await probe(cfg, transport=transport(handler))
    assert can is True


async def test_a_bare_model_id_is_an_openrouter_one(cfg):
    """The route rule the provider seam already uses (providers/openrouter.py):
    no prefix means OpenRouter, and the probe must not read `qwen` as a route."""
    cfg = cfg.model_copy(update={"chat_model": "qwen/qwen3-vl",
                                 "chat_image_input": "auto"})
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, json={"data": [
            {"id": "qwen/qwen3-vl",
             "architecture": {"input_modalities": ["text", "image"]}}]})

    can, _ = await probe(cfg, transport=transport(handler))
    assert can is True
    assert asked == ["https://openrouter.ai/api/v1/models"]


async def test_an_unreachable_server_means_no_paperclip_not_a_failure(cfg):
    cfg = cfg.model_copy(update={"chat_model": "lm_studio/who/knows",
                                 "chat_image_input": "auto"})

    def handler(request):
        raise httpx.ConnectError("nothing listening")

    can, why = await probe(cfg, transport=transport(handler))
    assert can is False and why                     # …and it says who didn't answer


async def test_the_override_answers_without_asking_anyone(cfg):
    """CHAT_IMAGE_INPUT is the escape hatch for a probe that guesses wrong, so
    it must short-circuit *before* the network — which is also what keeps the
    rest of this suite offline."""
    def handler(request):                            # pragma: no cover — must not run
        raise AssertionError("the override asked the provider anyway")

    on = cfg.model_copy(update={"chat_model": "lm_studio/x", "chat_image_input": "on"})
    off = cfg.model_copy(update={"chat_model": "lm_studio/x", "chat_image_input": "off"})
    assert (await probe(on, transport=transport(handler)))[0] is True
    assert (await probe(off, transport=transport(handler)))[0] is False


async def test_no_model_configured_is_no_pictures(cfg):
    can, why = await probe(cfg.model_copy(update={"chat_image_input": "auto"}))
    assert can is False and "no chat model" in why


# ---- the shelf: world/uploads.py -------------------------------------------

def test_a_saved_picture_is_shrunk_and_re_encoded(tmp_path):
    shelf = Uploads(tmp_path, max_px=64)
    saved = shelf.save(png(400, 200))

    assert (saved.width, saved.height) == (64, 32)      # the long side, capped
    assert saved.media_type == "image/jpeg"             # no alpha → the small one
    assert saved.path.read_bytes()[:2] == b"\xff\xd8"   # …and it really is one
    assert saved.url == f"/api/uploads/{saved.id}"


def test_transparency_survives_as_png(tmp_path):
    shelf = Uploads(tmp_path, max_px=64)
    saved = shelf.save(png(40, 40, (0, 0, 0, 0), mode="RGBA"))
    assert saved.media_type == "image/png"


def test_a_picture_that_already_fits_is_not_upscaled(tmp_path):
    saved = Uploads(tmp_path, max_px=1024).save(png(40, 30))
    assert (saved.width, saved.height) == (40, 30)


def test_something_that_is_not_an_image_is_refused_in_words(tmp_path):
    with pytest.raises(UploadRejected):
        Uploads(tmp_path).save(b"this is a text file, not a picture")
    with pytest.raises(UploadRejected):
        Uploads(tmp_path).save(b"")


def test_the_shelf_keeps_only_the_newest(tmp_path):
    shelf = Uploads(tmp_path, max_px=32, keep=2)
    first = shelf.save(png())
    shelf.save(png())
    shelf.save(png())
    assert len(list(tmp_path.iterdir())) == 2
    assert shelf.get(first.id) is None                  # pruned, and honest about it


def test_a_name_cannot_climb_out_of_the_shelf(tmp_path):
    shelf = Uploads(tmp_path)
    shelf.save(png())
    assert shelf.get("../../etc/passwd") is None
    assert shelf.get("nope.jpg") is None


def test_the_data_url_carries_the_bytes_inline(tmp_path):
    shelf = Uploads(tmp_path)
    saved = shelf.save(png())
    url = shelf.data_url(saved)
    assert url.startswith(f"data:{saved.media_type};base64,")
    import base64
    assert base64.b64decode(url.split(",", 1)[1]) == saved.path.read_bytes()


# ---- assembly: where the picture joins the prompt ---------------------------

def test_the_image_part_rides_the_final_user_turn_only():
    messages = [{"role": "system", "content": "who she is"},
                {"role": "user", "content": "and this?"},
                {"role": "assistant", "content": "mm"},
                {"role": "user", "content": "what is this?"}]
    wired = asm.with_image(messages, "data:image/png;base64,AAAA")

    assert wired[-1]["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    assert wired[1]["content"] == "and this?"           # the earlier turn is text
    assert messages[-1]["content"] == "what is this?"   # …and the record is untouched


def test_a_picture_with_no_words_still_makes_a_well_formed_turn():
    wired = asm.with_image([{"role": "user", "content": "  "}], "data:x")
    assert wired[0]["content"][0]["text"] == asm.IMAGE_ONLY_TEXT


def test_the_note_is_what_survives_the_turn():
    """The bytes ride one prompt; the *record* — the corpus line and the window
    a few turns later — keeps a note, or her reply dangles from nothing."""
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "what is this?"}]
    asm.mark_picture(messages)
    assert asm.PICTURE_NOTE in messages[-1]["content"]
    assert asm.note_picture("") == asm.PICTURE_NOTE


def test_the_context_meter_charges_for_a_picture():
    text_only = [{"role": "user", "content": "what is this?"}]
    with_picture = asm.with_image(text_only, "data:image/png;base64," + "A" * 40_000)
    # not four characters per base64 token — that would read as 10k for a thumbnail
    assert estimate_messages(with_picture) - estimate_messages(text_only) == \
        pytest.approx(IMAGE_TOKENS, abs=4)


# ---- the whole path, over the real routes -----------------------------------

@pytest.fixture
def seeing(cfg):
    """An app whose model can be shown pictures, without asking anyone."""
    return cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                  "chat_image_input": "on"})


def test_a_picture_reaches_the_brain_and_the_transcript(seeing):
    brain = FakeBrain()
    app = create_app(seeing, brain=brain)
    with TestClient(app) as client:
        up = client.post("/api/uploads",
                         files={"file": ("bike.png", png(300, 200), "image/png")})
        assert up.status_code == 200
        picture = up.json()
        assert picture["url"] == f"/api/uploads/{picture['id']}"

        r = client.post("/api/chat", json={"text": "what is this?", "channel": "cli",
                                           "image_id": picture["id"]})
        assert r.status_code == 200
        # the model was handed the bytes, inline
        assert brain.images == [f"data:{picture['media_type']};base64," +
                                brain.images[0].split(",", 1)[1]]
        assert brain.images[0].startswith("data:image/")
        # …and every room sees what was sent, beside the words
        user_entry = r.json()["user_message"]
        assert user_entry["image_url"] == picture["url"]
        assert user_entry["text"] == "what is this?"
        # the file itself is served back for the transcript to render
        served = client.get(picture["url"])
        assert served.status_code == 200
        assert served.headers["content-type"] == picture["media_type"]


def test_a_picture_with_no_words_is_a_turn(seeing):
    brain = FakeBrain()
    app = create_app(seeing, brain=brain)
    with TestClient(app) as client:
        picture = client.post(
            "/api/uploads", files={"file": ("x.png", png(), "image/png")}).json()
        r = client.post("/api/chat", json={"text": "", "channel": "cli",
                                           "image_id": picture["id"]})
        assert r.status_code == 200                    # not "not a meaningful turn"
        assert brain.images[0].startswith("data:image/")


def test_a_picture_that_is_gone_is_said_so_not_sent_without(seeing):
    brain = FakeBrain()
    with TestClient(create_app(seeing, brain=brain)) as client:
        r = client.post("/api/chat", json={"text": "look", "channel": "cli",
                                           "image_id": "nothere.jpg"})
    assert r.status_code == 404
    assert brain.images == []                          # the words did not go alone


def test_a_text_only_model_refuses_the_upload_and_the_turn(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                 "chat_image_input": "off"})
    with TestClient(create_app(cfg, brain=FakeBrain())) as client:
        up = client.post("/api/uploads",
                         files={"file": ("x.png", png(), "image/png")})
        assert up.status_code == 409
        turn = client.post("/api/chat", json={"text": "look", "channel": "cli",
                                              "image_id": "anything.jpg"})
        assert turn.status_code == 409


def test_a_file_that_is_not_a_picture_is_refused_at_the_door(seeing):
    with TestClient(create_app(seeing, brain=FakeBrain())) as client:
        r = client.post("/api/uploads",
                        files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 415


def test_an_oversized_picture_is_refused_by_size_not_by_decoding(cfg):
    cfg = cfg.model_copy(update={"tools_backend": "off", "mind_enabled": False,
                                 "chat_image_input": "on", "upload_max_bytes": 500})
    with TestClient(create_app(cfg, brain=FakeBrain())) as client:
        r = client.post("/api/uploads",
                        files={"file": ("big.png", png(400, 400), "image/png")})
    assert r.status_code == 413


def test_the_rooms_learn_the_capability_off_the_bus(seeing):
    """It rides the hub as sticky state, so a page that opens later — and a room
    already open when the model is swapped — both get the answer (SPEC §10)."""
    app = create_app(seeing, brain=FakeBrain())
    with TestClient(app) as client:
        rt = app.state.rt
        rt.loop.call_soon_threadsafe(rt.stopping.set)
        events = [json.loads(line[len("data: "):])
                  for line in client.get("/api/events").text.splitlines()
                  if line.startswith("data: ")]
    capability = [e for e in events if e["type"] == "capabilities"]
    assert capability and capability[0]["image_input"] is True


def test_the_voice_room_keeps_her_voice_when_you_send_a_picture(seeing):
    """The socket carries the id, not the bytes — which is what lets an image
    turn stay on the path that has TTS on the end of it (routes/voice_ws.py).
    Going around to HTTP would have cost her the voice for that one turn."""
    brain = FakeBrain()
    app = create_app(seeing, brain=brain)
    with TestClient(app) as client:
        picture = client.post(
            "/api/uploads", files={"file": ("x.png", png(200, 100), "image/png")}
        ).json()
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            while ws.receive_json()["type"] != "session":
                pass
            while ws.receive_json()["type"] != "done":      # her greeting
                pass
            ws.send_json({"type": "text", "text": "what is this?",
                          "image_id": picture["id"]})
            spoken = []
            while True:
                message = ws.receive_json()
                if message["type"] in ("audio", "filler") and message.get("text"):
                    spoken.append(message["text"])
                if message["type"] in ("done", "error", "cancelled"):
                    break
        assert message["type"] == "done" and spoken       # she answered, out loud
        assert brain.images[-1].startswith("data:image/")
        sent = [m for m in app.state.rt.transcript if m["role"] == "user"][-1]
        assert sent["image_url"] == picture["url"]


def test_a_picture_the_socket_cannot_find_is_refused_not_dropped(seeing):
    brain = FakeBrain()
    app = create_app(seeing, brain=brain)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_json({"type": "hello", "session_id": None})
            while ws.receive_json()["type"] != "session":
                pass
            while ws.receive_json()["type"] != "done":
                pass
            ws.send_json({"type": "text", "text": "look", "image_id": "gone.jpg"})
            assert ws.receive_json()["type"] == "rejected"
        assert brain.images == []                  # the words did not go alone
