"""The spawn wire between the host and her hands (SPEC §7.2).

Her tool server is a separate process, and the only thing that reaches it is a
dict of strings. That dict used to be written in `world/main.py` and read in
`world/tools/server.py`, each with its own spelling of every key and its own
opinion of what an absent one meant — and nothing failed when they disagreed.

`ToolServerEnv` is both sides now, so these tests are the thing neither file
could do on its own: encode, cross, decode, and land on the same values.
"""
from __future__ import annotations

import pytest

from yurios.world.tools.spawn_env import KEYS, SETTINGS, ToolServerEnv


def test_a_config_survives_the_crossing_unchanged(cfg):
    """The property the whole module exists for: what the host meant is what
    the server gets. A key misspelled on either side breaks this."""
    sent = ToolServerEnv.from_config(cfg)
    assert ToolServerEnv.from_environ(sent.to_environ()) == sent


@pytest.mark.parametrize("update", [
    {},
    {"selfie_backend": "mock", "search_backend": "searxng",
     "search_results": 12, "search_safesearch": 2, "fetch_timeout_s": 2.5,
     "fetch_max_bytes": 512, "research_max_pages": 1, "timer_max_minutes": 15},
    {"workspace_enabled": False, "skills_enabled": False, "mind_enabled": False},
    {"selfie_templates": "/tmp/hers.yaml", "selfie_templates_extra": "/tmp/x.yaml",
     "searxng_url": "http://elsewhere:9", "search_language": "ja"},
])
def test_every_knob_survives_it_too(cfg, update):
    """…including the ones nothing else in the suite ever sets to a non-default.
    A `str()`/parse pair that is asymmetric for one field only shows up here."""
    sent = ToolServerEnv.from_config(cfg.model_copy(update=update))
    assert ToolServerEnv.from_environ(sent.to_environ()) == sent


def test_every_setting_actually_crosses(cfg):
    """A field added to the dataclass and forgotten in `to_environ` would read
    back as its default forever — silently, and only in production, because a
    test that builds the object directly never crosses anything.

    So: move every knob off its default at least once, cross, and require the
    move to survive. A field that never crosses can never differ.
    """
    default = ToolServerEnv()
    moved: set[str] = set()
    for update in ({"selfie_backend": "off", "search_backend": "searxng",
                    "search_results": 12, "search_safesearch": 2,
                    "fetch_timeout_s": 2.5, "fetch_max_bytes": 512,
                    "research_max_pages": 1, "timer_max_minutes": 15,
                    "selfie_templates": "/tmp/hers.yaml",
                    "selfie_templates_extra": "/tmp/x.yaml",
                    "searxng_url": "http://elsewhere:9",
                    "search_language": "ja", "mind_enabled": True},
                   {"workspace_enabled": False, "skills_enabled": False}):
        sent = ToolServerEnv.from_config(cfg.model_copy(update=update))
        landed = ToolServerEnv.from_environ(sent.to_environ())
        moved |= {name for name in SETTINGS
                  if getattr(landed, name) != getattr(default, name)}
    assert moved == set(SETTINGS), \
        f"never seen to cross: {sorted(set(SETTINGS) - moved)}"
    assert len(KEYS) == len(SETTINGS)


def test_an_absent_key_is_the_standalone_servers_answer_not_a_configs():
    """`python -m yurios.world.tools.server` has no host: no camera to reach
    through, no Vault, no mind reading a self-edit queue. Those three defaults
    differ from what a Config produces *on purpose*, and this is where that is
    written down rather than inferred from two files disagreeing."""
    bare = ToolServerEnv.from_environ({})
    assert bare == ToolServerEnv()
    assert bare.selfies is True            # describable; the host takes the photo
    assert bare.vault_path is None         # no desk, so the desk tools are absent
    assert bare.selfedit is False          # nothing would ever read the queue


def test_zero_is_off_and_everything_else_is_on():
    """The flag convention the host writes to, pinned: `0` is the only off."""
    on = ToolServerEnv.from_environ({"WORKSPACE_ENABLED": "1",
                                     "SKILLS_ENABLED": "true",
                                     "SELFEDIT_ENABLED": ""})
    assert (on.workspace, on.skills, on.selfedit) == (True, True, True)
    off = ToolServerEnv.from_environ({"WORKSPACE_ENABLED": "0",
                                      "SKILLS_ENABLED": "0",
                                      "SELFIE_ENABLED": "0"})
    assert (off.workspace, off.skills, off.selfies) == (False, False, False)


def test_a_value_that_will_not_parse_refuses_to_start():
    """The host never writes one, so a malformed number was exported by hand —
    and a server quietly running on a substituted number is worse than one that
    will not come up."""
    with pytest.raises(ValueError):
        ToolServerEnv.from_environ({"FETCH_MAX_BYTES": "lots"})


def test_a_whole_number_crosses_without_a_decimal_point():
    """Read by a human running `env` against a stuck tool server."""
    assert ToolServerEnv().to_environ()["TIMER_MAX_MINUTES"] == "180"
    assert ToolServerEnv().to_environ()["FETCH_TIMEOUT_S"] == "8"
    assert ToolServerEnv(fetch_timeout_s=2.5).to_environ()["FETCH_TIMEOUT_S"] == "2.5"


def test_neither_side_reads_the_environment_behind_the_wire():
    """The whole point. `spawn_env.py` is the one module that names these keys;
    a fresh `os.environ.get("SEARCH_...")` in either file is the drift coming
    back, so it fails here rather than in six months on somebody's machine."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ("yurios/world/tools/server.py", "yurios/world/main.py"):
        source = (root / rel).read_text()
        for key in KEYS:
            assert f'"{key}"' not in source, \
                f"{rel} names {key} itself — it belongs in tools/spawn_env.py"
