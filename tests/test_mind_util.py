"""The reverse JSONL readers (yurios/mind/util.py).

These back every paged view on the mind debug page, so the edges that matter
are the ones a log actually hits in the wild: a torn tail line after a crash, a
file smaller than one read block, a file that was never created, and the exact
page boundaries a pager walks.
"""
from __future__ import annotations

import json

import pytest

from yurios.mind.util import jsonl_append, jsonl_count, jsonl_page


def write(path, rows, *, trailing_newline=True):
    text = "".join(json.dumps(r) + "\n" for r in rows)
    if not trailing_newline and text.endswith("\n"):
        text = text[:-1]
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_is_empty_not_an_error(tmp_path):
    assert jsonl_page(tmp_path / "nope.jsonl") == ([], False, 0)
    assert jsonl_count(tmp_path / "nope.jsonl") == 0


def test_empty_file_is_empty(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert jsonl_page(path) == ([], False, 0)


def test_pages_newest_first_across_exact_boundaries(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": i} for i in range(55)])

    first, more, total = jsonl_page(path, page=0, limit=25)
    assert [r["i"] for r in first] == list(range(54, 29, -1))
    assert (more, total) == (True, 55)

    second, more, _ = jsonl_page(path, page=1, limit=25)
    assert [r["i"] for r in second] == list(range(29, 4, -1))
    assert more is True

    third, more, _ = jsonl_page(path, page=2, limit=25)
    assert [r["i"] for r in third] == [4, 3, 2, 1, 0]
    assert more is False

    # no row appears on two pages, and none is skipped between them
    seen = [r["i"] for r in first + second + third]
    assert seen == list(range(54, -1, -1))


def test_page_past_the_end_is_empty(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": i} for i in range(3)])
    assert jsonl_page(path, page=9, limit=25) == ([], False, 3)


def test_exact_multiple_of_limit_reports_no_more_on_the_last_page(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": i} for i in range(50)])
    _, more, _ = jsonl_page(path, page=0, limit=25)
    assert more is True
    last, more, _ = jsonl_page(path, page=1, limit=25)
    assert more is False
    assert [r["i"] for r in last] == list(range(24, -1, -1))


def test_no_trailing_newline_still_yields_the_last_row(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": 0}, {"i": 1}], trailing_newline=False)
    rows, _, _ = jsonl_page(path)
    assert [r["i"] for r in rows] == [1, 0]


def test_a_torn_tail_line_is_skipped(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": 0}, {"i": 1}])
    with path.open("a", encoding="utf-8") as f:
        f.write('{"i": 2, "text": "half a rec')      # crash mid-write
    rows, _, total = jsonl_page(path)
    assert [r["i"] for r in rows] == [1, 0]
    assert total == 2                                 # the torn line has no newline


def test_rows_spanning_many_read_blocks(tmp_path):
    """Records far larger than the 64 KiB block, so a line is stitched across
    several backwards reads."""
    rows = [{"i": i, "pad": "x" * 40_000} for i in range(12)]
    path = write(tmp_path / "big.jsonl", rows)
    got, more, total = jsonl_page(path, page=0, limit=5)
    assert [r["i"] for r in got] == [11, 10, 9, 8, 7]
    assert (more, total) == (True, 12)
    tail, more, _ = jsonl_page(path, page=2, limit=5)
    assert [r["i"] for r in tail] == [1, 0]
    assert more is False


def test_file_smaller_than_one_block(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": 0}])
    assert jsonl_page(path)[0] == [{"i": 0}]


def test_non_dict_and_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"i": 0}\n\n[1,2,3]\n"just a string"\n{"i": 1}\n', encoding="utf-8")
    rows, _, _ = jsonl_page(path)
    assert [r["i"] for r in rows] == [1, 0]


def test_match_filters_and_suppresses_the_total(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": i, "kind": "a" if i % 2 else "b"}
                                        for i in range(10)])
    rows, more, total = jsonl_page(path, limit=3, match=lambda r: r["kind"] == "a")
    assert [r["i"] for r in rows] == [9, 7, 5]
    assert more is True
    assert total is None, "counting a filtered file means a full pass; the UI says 3+"


def test_shape_runs_before_rows_are_collected(tmp_path):
    path = write(tmp_path / "t.jsonl", [{"i": i, "heavy": "x" * 100} for i in range(3)])
    rows, _, _ = jsonl_page(path, shape=lambda r: {"i": r["i"], "n": len(r["heavy"])})
    assert rows == [{"i": 2, "n": 100}, {"i": 1, "n": 100}, {"i": 0, "n": 100}]
    assert all("heavy" not in r for r in rows)


@pytest.mark.parametrize("page,limit", [(-1, 25), (0, 0), (-5, -5)])
def test_nonsense_paging_is_clamped_not_fatal(tmp_path, page, limit):
    path = write(tmp_path / "t.jsonl", [{"i": i} for i in range(3)])
    rows, _, _ = jsonl_page(path, page=page, limit=limit)
    assert rows and rows[0]["i"] == 2


def test_count_tracks_appends(tmp_path):
    """The count is cached on (size, mtime_ns); an append must invalidate it."""
    path = tmp_path / "t.jsonl"
    jsonl_append(path, {"i": 0})
    assert jsonl_count(path) == 1
    jsonl_append(path, {"i": 1})
    assert jsonl_count(path) == 2


def test_append_serialises_unserialisable_values_instead_of_raising(tmp_path):
    """A log must never raise into the path it observes (world/tools/guard.py)."""
    path = tmp_path / "t.jsonl"
    jsonl_append(path, {"where": tmp_path / "somewhere"})
    rows, _, _ = jsonl_page(path)
    assert rows[0]["where"] == str(tmp_path / "somewhere")


def test_rotation_leaves_the_live_file_fresh(tmp_path):
    path = tmp_path / "t.jsonl"
    for i in range(50):
        jsonl_append(path, {"i": i, "pad": "x" * 100}, max_bytes=1_000)
    assert (tmp_path / "t.jsonl.1").is_file()
    # the debug page reads only the live file, and must not see the rolled-over half
    rows, _, _ = jsonl_page(path)
    rolled, _, _ = jsonl_page(tmp_path / "t.jsonl.1")
    assert rows and rolled
    assert {r["i"] for r in rows}.isdisjoint({r["i"] for r in rolled})
