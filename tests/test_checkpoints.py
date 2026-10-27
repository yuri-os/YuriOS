"""Pictures → Checkpoints: Civitai for her local camera (SPEC §11.5).

Three layers, each against fakes: the header check that settles whether this
build loads a file (`sniff.verify_local_checkpoint`), the listing's sorting of
Civitai's models into what this build will do with them (`civitai.classify`),
and the host routes — the key into `.env`, the cached list, a download that
lands and resumes, and pointing the camera at a file. Civitai itself is an
httpx MockTransport; nothing here touches the network.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from yurios.characters import CharacterRegistry
from yurios.forge import civitai
from yurios.forge.backends.sniff import verify_local_checkpoint
from yurios.world.config import Config
from yurios.world.host import create_host_app

from tests.support.checkpoints import write_safetensors_header
from tests.test_host import fake_character_app, record

SDXL_KEYS = ["model.diffusion_model.input_blocks.0.0.weight", "conditioner.embedders.0.x",
             "first_stage_model.decoder.conv_in.weight"]
ANIMA_PROBE = "model.diffusion_model.blocks.0.adaln_modulation_mlp.1.weight"


def anima_keys(blocks=28):
    return ([f"model.diffusion_model.blocks.{i}.self_attn.q_proj.weight" for i in range(blocks)]
            + ["model.diffusion_model.llm_adapter.blocks.0.cross_attn.k_proj.weight", ANIMA_PROBE])


def krea2_file(path, layout="convrot_w4a4"):
    quant = {"format_version": "1.0", "layers": {
        "blocks.0.attn.qkv": {"format": layout}, "blocks.1.attn.qkv": {"format": layout}}}
    return write_safetensors_header(path, ["blocks.0.attn.qkv.weight", "txtfusion.x"],
                                    {"_quantization_metadata": json.dumps(quant)})


# --- does this build load it? --------------------------------------------------

def test_an_sdxl_file_loads_and_a_v_prediction_one_says_why_it_may_not_look_right(tmp_path):
    assert verify_local_checkpoint(write_safetensors_header(tmp_path / "a.safetensors",
                                                            SDXL_KEYS)).status == "ok"
    vpred = verify_local_checkpoint(write_safetensors_header(tmp_path / "b.safetensors",
                                                             [*SDXL_KEYS, "v_pred"]))
    assert (vpred.architecture, vpred.status) == ("sdxl", "warn")
    assert vpred.usable and "v-prediction" in vpred.note


def test_krea2_loads_only_in_the_one_int4_layout_the_backend_runs(tmp_path):
    assert verify_local_checkpoint(krea2_file(tmp_path / "a.safetensors")).status == "ok"
    int8 = verify_local_checkpoint(krea2_file(tmp_path / "b.safetensors", "int8_tensorwise"))
    assert int8.status == "unsupported" and "int8_tensorwise" in int8.note
    assert not int8.usable


def test_anima_loads_only_at_the_2b_shape_the_backend_builds(tmp_path):
    ok = write_safetensors_header(tmp_path / "a.safetensors", anima_keys(),
                                  shapes={ANIMA_PROBE: [256, 2048]})
    assert verify_local_checkpoint(ok).status == "ok"
    bigger = write_safetensors_header(tmp_path / "b.safetensors", anima_keys(36),
                                      shapes={ANIMA_PROBE: [256, 2560]})
    verdict = verify_local_checkpoint(bigger)
    assert verdict.status == "unsupported" and "36 blocks" in verdict.note


def test_a_file_that_is_not_a_checkpoint_is_refused(tmp_path):
    junk = tmp_path / "junk.safetensors"
    junk.write_bytes(b"<html>sign in</html>")
    assert verify_local_checkpoint(junk).status == "unsupported"


# --- the listing, sorted -------------------------------------------------------

def listed(model_id, name, base, files, *, version="v1", nsfw=False, downloads=10, images=()):
    return {"id": model_id, "name": name, "nsfw": nsfw, "stats": {"downloadCount": downloads},
            "creator": {"username": "someone"},
            "modelVersions": [{"id": model_id * 10, "name": version, "baseModel": base,
                               "publishedAt": "2026-10-25T00:00:00Z", "images": list(images),
                               "files": [{"name": n, "sizeKB": kb, "type": "Model",
                                          "primary": i == 0, "metadata": {"fp": "fp16"},
                                          "downloadUrl": f"https://civitai.com/api/download/models/{model_id * 10}"}
                                         for i, (n, kb) in enumerate(files)]}]}


FAM = {f.base_model: f for f in civitai.FAMILIES}


def test_each_family_is_sorted_by_what_its_backend_will_do():
    gb = 1048576
    cases = [
        (listed(1, "Ill", "Illustrious", [("ill.safetensors", 6.5 * gb)]), "Illustrious", "best"),
        (listed(2, "Vp", "NoobAI", [("noob_vpred.safetensors", 6.5 * gb)]), "NoobAI", "caution"),
        (listed(3, "Pony", "Pony", [("pony.safetensors", 6.5 * gb)]), "Pony", "works"),
        (listed(4, "Small", "Anima", [("a.safetensors", 3.9 * gb)]), "Anima", "best"),
        (listed(5, "Big", "Anima", [("b.safetensors", 5.4 * gb)]), "Anima", "no"),
        (listed(6, "Conv", "Krea 2", [("k_int8.safetensors", 12 * gb),
                                      ("k_int4_convrot.safetensors", 7 * gb)]), "Krea 2", "best"),
        (listed(7, "Int4", "Krea 2", [("k_int4.safetensors", 7 * gb)]), "Krea 2", "likely"),
        (listed(8, "SVDQuant", "Krea 2", [("k_int4.safetensors", 7 * gb)]), "Krea 2", "no"),
    ]
    for model, base, status in cases:
        card = civitai.classify(model, FAM[base])
        assert card["status"] == status, model["name"]
    # the INT4 file is the one picked, not the primary INT8
    assert civitai.classify(cases[5][0], FAM["Krea 2"])["file"] == "k_int4_convrot.safetensors"


def test_weights_filed_as_a_diffusion_model_count_and_a_text_encoder_does_not():
    model = listed(10, "Conv", "Krea 2", [("k_int4_convrot.safetensors", 7 * 1048576)])
    model["modelVersions"][0]["files"][0]["type"] = "Diffusion Model"
    assert civitai.classify(model, FAM["Krea 2"])["file"] == "k_int4_convrot.safetensors"
    anima = listed(11, "A", "Anima", [("a_txt.safetensors", 2.2 * 1048576),
                                      ("a.safetensors", 3.9 * 1048576)])
    anima["modelVersions"][0]["files"][0].update(type="Text Encoder")
    assert civitai.classify(anima, FAM["Anima"])["file"] == "a.safetensors"


def test_a_krea2_model_with_only_formats_the_backend_cant_read_is_left_out():
    model = listed(9, "Fp8", "Krea 2", [("k_fp8.safetensors", 12 * 1048576),
                                         ("k.gguf", 7 * 1048576)])
    assert civitai.classify(model, FAM["Krea 2"]) is None


def test_previews_keep_their_rating_and_shrink_to_a_thumbnail():
    images = [{"url": "https://image.civitai.com/x/original=true/1.jpeg", "nsfwLevel": 1,
               "type": "image"},
              {"url": "https://image.civitai.com/x/original=true/2.jpeg", "nsfwLevel": 8,
               "type": "image"},
              {"url": "https://image.civitai.com/x/original=true/3.mp4", "nsfwLevel": 1,
               "type": "video"}]
    card = civitai.classify(listed(1, "Ill", "Illustrious", [("i.safetensors", 1)],
                                   images=images), FAM["Illustrious"])
    assert [(i["nsfw_level"], "/width=450/" in i["url"]) for i in card["images"]] == \
        [(1, True), (8, True)]


def test_a_version_listed_twice_is_filed_under_the_first_listing():
    model = listed(1, "Ill", "Illustrious", [("i.safetensors", 1)])
    cards = civitai.build_catalog([(FAM["Illustrious"], "top", [model]),
                                   (FAM["Illustrious"], "week", [model])])
    assert [c["source"] for c in cards] == ["top"]


def test_a_searched_model_is_filed_under_a_family_the_camera_loads_or_says_why_not():
    both = listed(20, "Both", "Flux.1 D", [("flux.safetensors", 1)])
    both["modelVersions"].append({**listed(20, "Both", "Illustrious", [("ill.safetensors", 1)])
                                  ["modelVersions"][0], "id": 201})
    assert (civitai.classify_any(both)["family"], civitai.classify_any(both)["file"]) == \
        ("SDXL · Illustrious", "ill.safetensors")
    flux = civitai.classify_any(listed(21, "Flux", "Flux.1 D", [("f.safetensors", 1)]))
    assert (flux["family"], flux["base_model"], flux["status"]) == \
        (civitai.OTHER_FAMILY, "Flux.1 D", "no")
    fp8 = civitai.classify_any(listed(22, "Fp8", "Krea 2", [("k_fp8.safetensors", 1)]))
    assert (fp8["family"], fp8["status"]) == ("Krea 2", "no")
    assert civitai.classify_any(listed(23, "Gguf", "Flux.1 D", [("f.gguf", 1)])) is None


def test_what_a_search_found_joins_the_cache_and_outlives_a_refresh():
    listed_card = {"id": 1, "version_id": 10, "name": "Top", "source": "top"}
    found = [{"id": 1, "version_id": 10, "name": "Top"}, {"id": 2, "version_id": 20, "name": "New"}]
    catalog, added = civitai.merge_search({"fetched_at": 5, "models": [listed_card]}, found, "q")
    assert added == 1
    assert [(m["name"], m["source"], m["queries"]) for m in catalog["models"]] == [
        ("Top", "top", ["q"]), ("New", "search", ["q"])]
    again, added = civitai.merge_search(catalog, found, "q")
    assert added == 0 and again["models"][0]["queries"] == ["q"]
    fresh = civitai.carry_searches(again, {"fetched_at": 9, "models": [
        {"id": 1, "version_id": 11, "name": "Top", "source": "top"}]})
    assert [(m["version_id"], m["source"], m["queries"]) for m in fresh["models"]] == [
        (11, "top", ["q"]), (20, "search", ["q"])]
    assert civitai.normalize_query("  Hidden\t GEM ") == "hidden gem"


# --- the routes ------------------------------------------------------------------


def checkpoint_bytes(tmp_path):
    """A complete, tiny SDXL file, as a download would deliver it."""
    path = write_safetensors_header(tmp_path / "src.safetensors", SDXL_KEYS)
    return path.read_bytes()


def civitai_listing(request: httpx.Request) -> httpx.Response:
    if request.url.params.get("query"):
        gem = listed(3, "Hidden gem", "Illustrious", [("gem.safetensors", 1)], nsfw=True)
        if request.url.params.get_list("baseModels"):          # inside the camera's families
            return httpx.Response(200, json={"items": [
                listed(1, "Ready one", "Illustrious", [("ready.safetensors", 1)]), gem]})
        return httpx.Response(200, json={"items": [               # across every base model
            gem, listed(4, "Fluxy", "Flux.1 D", [("fluxy.safetensors", 1)])]})
    base = request.url.params.get("baseModels")
    if base == "Illustrious" and request.url.params.get("period") is None:
        return httpx.Response(200, json={"items": [
            listed(1, "Ready one", "Illustrious", [("ready.safetensors", 1)]),
        ]})
    if base == "Anima" and request.url.params.get("period") is None:
        return httpx.Response(200, json={"items": [
            listed(2, "Too big", "Anima", [("big.safetensors", 5.4 * 1048576)], nsfw=True),
        ]})
    return httpx.Response(200, json={"items": []})


@pytest.fixture
def board(tmp_path, monkeypatch):
    from yurios.desktop.routes import settings as panel

    folder = tmp_path / "ckpt"
    folder.mkdir()
    env = tmp_path / ".env"
    env.write_text(f"SELFIE_BACKEND=off\nSELFIE_CHECKPOINT_DIR={folder}\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_PATH", env)
    monkeypatch.setattr("yurios.world.host.hosting.create_app", fake_character_app)
    monkeypatch.setattr(civitai, "ASYNC_TRANSPORT", httpx.MockTransport(civitai_listing))
    payload = checkpoint_bytes(tmp_path)
    seen = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if request.headers.get("authorization") != "Bearer secret-key":
            return httpx.Response(401)
        start = int(request.headers.get("range", "bytes=0-")[6:-1] or 0)
        if start:
            return httpx.Response(206, content=payload[start:],
                                  headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, content=payload,
                              headers={"content-type": "application/octet-stream"})

    monkeypatch.setattr(civitai, "TRANSPORT", httpx.MockTransport(serve))
    registry = CharacterRegistry(tmp_path)
    registry.add(record(tmp_path, "yuri", enabled=False))
    # _env_file=None: this machine's .env may hold a real CIVITAI_API_KEY
    app = create_host_app(Config(_env_file=None, data_dir=tmp_path), registry)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, env=env, folder=folder, payload=payload,
                              seen=seen, root=tmp_path)


def wait_for(client, version_id, state="done", timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = client.get("/api/house/checkpoints/downloads").json()["downloads"]
        job = next((j for j in jobs if j["version_id"] == version_id), None)
        if job and job["state"] not in ("queued", "downloading"):
            assert job["state"] == state, job
            return job
        time.sleep(0.02)
    raise AssertionError("the download never finished")


def test_the_key_is_saved_to_env_and_can_be_taken_out_again(board):
    assert board.client.get("/api/house/checkpoints").json()["key_configured"] is False
    assert board.client.put("/api/house/civitai-key", json={"key": "has space"}).status_code == 422
    assert board.client.put("/api/house/civitai-key",
                            json={"key": "secret-key"}).json()["key_configured"] is True
    assert dotenv_values(board.env)["CIVITAI_API_KEY"] == "secret-key"
    # no restart: the next call already has it
    assert board.client.get("/api/house/checkpoints").json()["key_configured"] is True
    board.client.put("/api/house/civitai-key", json={"key": None})
    assert not dotenv_values(board.env).get("CIVITAI_API_KEY")


def test_the_list_is_fetched_on_request_and_cached(board):
    first = board.client.get("/api/house/checkpoints").json()
    assert first["catalog"] is None                 # opening the page fetches nothing
    catalog = board.client.post("/api/house/checkpoints/refresh").json()
    assert {(m["name"], m["status"], m["nsfw"]) for m in catalog["models"]} == {
        ("Ready one", "best", False), ("Too big", "no", True)}
    assert catalog["stale"] is False
    again = board.client.get("/api/house/checkpoints").json()["catalog"]
    assert [m["version_id"] for m in again["models"]] == [m["version_id"] for m in catalog["models"]]


def test_a_download_lands_in_the_folder_checked_and_then_sets_her_camera(board):
    board.client.post("/api/house/checkpoints/refresh")
    assert board.client.post("/api/house/checkpoints/downloads",
                             json={"version_id": 10}).status_code == 409     # no key yet
    board.client.put("/api/house/civitai-key", json={"key": "secret-key"})
    assert board.client.post("/api/house/checkpoints/downloads",
                             json={"version_id": 10}).status_code == 200
    job = wait_for(board.client, 10)
    assert job["verdict"]["status"] == "ok"
    assert (board.folder / "ready.safetensors").read_bytes() == board.payload
    assert not (board.folder / "ready.safetensors.part").exists()

    view = board.client.get("/api/house/checkpoints").json()
    assert [f["file"] for f in view["local"]["files"]] == ["ready.safetensors"]
    assert board.client.post("/api/house/checkpoints/downloads",
                             json={"version_id": 10}).status_code == 409     # already here

    used = board.client.post("/api/house/checkpoints/use", json={"file": "ready.safetensors"})
    assert used.status_code == 200 and used.json()["restart_required"] is True
    env = dotenv_values(board.env)
    assert env["SELFIE_LOCAL_MODEL"] == str(board.folder / "ready.safetensors")
    assert env["SELFIE_BACKEND"] == "diffusers"      # was off: the local renderer now
    assert board.client.get("/api/house/checkpoints").json()["current"]["pending"] is True


def test_a_download_stopped_halfway_resumes_where_it_left_off(board):
    board.client.post("/api/house/checkpoints/refresh")
    board.client.put("/api/house/civitai-key", json={"key": "secret-key"})
    half = len(board.payload) // 2
    (board.folder / "ready.safetensors.part").write_bytes(board.payload[:half])
    assert board.client.get("/api/house/checkpoints").json()["local"]["partial"] == {
        "ready.safetensors": half}
    board.client.post("/api/house/checkpoints/downloads", json={"version_id": 10})
    wait_for(board.client, 10)
    assert board.seen[-1]["range"] == f"bytes={half}-"
    assert (board.folder / "ready.safetensors").read_bytes() == board.payload


def test_what_cant_be_fetched_or_used_is_refused_with_a_reason(board):
    board.client.post("/api/house/checkpoints/refresh")
    board.client.put("/api/house/civitai-key", json={"key": "secret-key"})
    assert board.client.post("/api/house/checkpoints/downloads",
                             json={"version_id": 20}).status_code == 422     # Anima 2.9B
    assert board.client.post("/api/house/checkpoints/downloads",
                             json={"version_id": 999}).status_code == 404
    assert board.client.post("/api/house/checkpoints/use",
                             json={"file": "../.env"}).status_code == 422
    krea2_file(board.folder / "int8.safetensors", "int8_tensorwise")
    refused = board.client.post("/api/house/checkpoints/use", json={"file": "int8.safetensors"})
    assert refused.status_code == 422 and "ConvRot" in refused.json()["detail"]
    assert "SELFIE_LOCAL_MODEL" not in dotenv_values(board.env)


def test_a_wrong_key_is_a_failed_download_that_says_so(board):
    board.client.post("/api/house/checkpoints/refresh")
    board.client.put("/api/house/civitai-key", json={"key": "wrong"})
    board.client.post("/api/house/checkpoints/downloads", json={"version_id": 10})
    job = wait_for(board.client, 10, state="failed")
    assert "API key" in job["error"]
    assert not (board.folder / "ready.safetensors").exists()


def test_a_search_asks_civitai_and_its_answer_is_cached_and_downloadable(board):
    board.client.post("/api/house/checkpoints/refresh")
    assert board.client.post("/api/house/checkpoints/search", json={"query": " x "}).status_code == 422
    answer = board.client.post("/api/house/checkpoints/search",
                               json={"query": "  Hidden   GEM "}).json()
    assert (answer["query"], answer["found"], answer["added"]) == ("hidden gem", 3, 2)
    cards = {m["name"]: m for m in answer["catalog"]["models"]}
    assert (cards["Hidden gem"]["source"], cards["Hidden gem"]["status"]) == ("search", "best")
    assert (cards["Fluxy"]["family"], cards["Fluxy"]["status"]) == (civitai.OTHER_FAMILY, "no")
    assert (cards["Ready one"]["source"], cards["Ready one"]["queries"]) == ("top", ["hidden gem"])
    # cached: the page has it without asking again, and a refresh keeps it
    board.client.post("/api/house/checkpoints/refresh")
    page = board.client.get("/api/house/checkpoints").json()
    assert civitai.OTHER_FAMILY in page["families"]
    kept = {m["name"]: m for m in page["catalog"]["models"]}
    assert kept["Hidden gem"]["queries"] == ["hidden gem"]
    # and a searched card downloads like a listed one
    board.client.put("/api/house/civitai-key", json={"key": "secret-key"})
    assert board.client.post("/api/house/checkpoints/downloads",
                             json={"version_id": 30}).status_code == 200
    wait_for(board.client, 30)
    assert (board.folder / "gem.safetensors").is_file()


def test_a_search_civitai_doesnt_answer_says_so(board, monkeypatch):
    monkeypatch.setattr(civitai, "ASYNC_TRANSPORT",
                        httpx.MockTransport(lambda request: httpx.Response(503)))
    answer = board.client.post("/api/house/checkpoints/search", json={"query": "anything"})
    assert answer.status_code == 502 and "503" in answer.json()["detail"]
    assert board.client.get("/api/house/checkpoints").json()["catalog"] is None
