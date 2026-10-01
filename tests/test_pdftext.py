"""A PDF handed to her shelf becomes the text the shelf reads (SPEC §20.1)."""
from __future__ import annotations

import io
import sys

import pytest

from yurios.mind.documents import DocumentRefused, shelf_document
from yurios.mind.knowledge import KnowledgeStore
from yurios.mind.pdftext import PdfUnreadable, pdf_text
from yurios.mind.vaultio import MindVault
from yurios.kernel.clock import VirtualClock

from .conftest import SIM_START, FakeEmbedder
from .pdfs import make_pdf

pypdf = pytest.importorskip("pypdf")

TEA = [
    "Gyokuro is shaded for three weeks before the har-",
    "vest, which raises theanine and gives the brew its",
    "savory depth.",
    "Bancha is the everyday cut, later flushes and larger",
    "leaves, more forgiving of hot water.",
]


def test_the_paragraphs_a_pdf_lost_are_put_back():
    """pypdf hands back lines; the chunker splits on blank lines. Unconverted,
    a page would be one chunk several times the budget."""
    text = pdf_text(make_pdf([TEA, ["The second page."]]))
    assert text.split("\n\n") == [
        "[page 1]",
        "Gyokuro is shaded for three weeks before the harvest, which raises "
        "theanine and gives the brew its savory depth.",
        "Bancha is the everyday cut, later flushes and larger leaves, more "
        "forgiving of hot water.",
        "[page 2]",
        "The second page.\n",
    ]


def test_the_heading_is_the_pdfs_title_or_its_file_name():
    assert pdf_text(make_pdf([TEA], title="Field Notes on Tea"),
                    title="tea").startswith("# Field Notes on Tea\n\n[page 1]")
    assert pdf_text(make_pdf([TEA]), title="tea").startswith("# tea\n\n")


def test_a_scan_with_no_text_layer_says_so():
    with pytest.raises(PdfUnreadable, match="OCR"):
        pdf_text(make_pdf([[], []]))


def test_a_file_that_is_not_a_pdf_says_so():
    with pytest.raises(PdfUnreadable, match="can be read"):
        pdf_text(b"%PDF-1.4\nthis is not really one")


def test_a_password_protected_pdf_says_so():
    writer = pypdf.PdfWriter(clone_from=io.BytesIO(make_pdf([TEA])))
    writer.encrypt("secret", algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(PdfUnreadable, match="password"):
        pdf_text(buf.getvalue())


def test_without_pypdf_a_pdf_is_refused_with_what_to_install(monkeypatch):
    """An install that predates the dependency still runs; it just says why."""
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(PdfUnreadable, match="pip install pypdf"):
        pdf_text(make_pdf([TEA]))


def test_a_pdf_goes_on_the_shelf_as_the_md_of_its_text():
    doc, text = shelf_document("Field Notes.PDF", make_pdf([TEA]))
    assert doc == "Field_Notes.md"
    assert text.startswith("# Field Notes\n\n[page 1]\n\nGyokuro")


@pytest.mark.parametrize("filename,data,status,match", [
    ("scan.pdf", make_pdf([[]]), 415, "OCR"),
    ("web-page.pdf", make_pdf([TEA]), 409, "her own research"),
    ("broken.pdf", b"not a pdf", 415, "can't read this PDF"),
])
def test_a_pdf_the_shelf_will_not_take_says_why(filename, data, status, match):
    with pytest.raises(DocumentRefused, match=match) as refused:
        shelf_document(filename, data)
    assert refused.value.status == status


def test_a_pdf_has_its_own_size_limit(monkeypatch):
    """The text limit would refuse most PDFs, which are mostly not text."""
    monkeypatch.setattr("yurios.mind.documents.MAX_TEXT_BYTES", 10)
    shelf_document("tea.pdf", make_pdf([TEA]))
    monkeypatch.setattr("yurios.mind.documents.MAX_PDF_BYTES", 10)
    with pytest.raises(DocumentRefused) as refused:
        shelf_document("tea.pdf", make_pdf([TEA]))
    assert refused.value.status == 413


async def test_a_shelved_pdf_is_read_and_cited_by_page(tmp_path):
    store = KnowledgeStore(MindVault(tmp_path / "vault"), FakeEmbedder(),
                           VirtualClock(start=SIM_START.timestamp()), chunk_chars=120)
    doc, text = shelf_document("tea.pdf", make_pdf([TEA, ["Sencha is steamed."]]))
    store.shelve(doc, text)
    await store.scan()
    chunks = store.inspect("tea.md")
    assert len(chunks) > 1, "reflowed paragraphs chunk like any other document"
    assert any("[page 2]" in c.text for c in chunks)
