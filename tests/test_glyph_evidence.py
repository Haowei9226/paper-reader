import copy

import pymupdf as fitz
import pytest

from reader.glyph_evidence import _page_evidence, confirmed_alias, confirmed_leading_hyphen
from .test_pdf_text_encoding import _encoded_pdf


def evidence_pdf(tmp_path, *, mapped="fi", text="\ufb01n", rotation=0):
    path = tmp_path / "embedded-proof.pdf"
    with fitz.open(stream=_encoded_pdf(text, {"\ufb01": mapped})) as doc:
        page = doc[0]
        page.set_rotation(rotation)
        chars = page.get_texttrace()[0]["chars"]
        main = list(fitz.Rect(chars[0][3]) * page.rotation_matrix)
        following = list(fitz.Rect(chars[-1][3]) * page.rotation_matrix)
        doc.save(path)
    return path, main, following


@pytest.mark.parametrize("rotation", [0, 90])
def test_alias_requires_embedded_glyph_spelling_exact_unpainted_suffix_and_next_glyph(tmp_path, rotation):
    path, main, following = evidence_pdf(tmp_path, rotation=rotation)
    assert confirmed_alias(path, 1, main, "fi", following, "n")
    assert not confirmed_alias(path, 1, main, "fl", following, "n")
    assert not confirmed_alias(path, 1, main, "fi", following, "m")
    assert not confirmed_alias(path, 1, following, "fi", main, "n")


def test_vertical_font_metric_difference_does_not_relax_horizontal_matching(tmp_path):
    path, main, following = evidence_pdf(tmp_path)
    shifted_y = [main[0], main[1] + .08, main[2], main[3] + .08]
    assert confirmed_alias(path, 1, shifted_y, "fi", following, "n")
    assert not confirmed_alias(path, 1, [main[0] + .1, main[1], main[2] + .1, main[3]], "fi", following, "n")
    assert not confirmed_alias(path, 1, [main[0], main[1] + 10, main[2], main[3] + 10], "fi", following, "n")


def test_ligature_without_trace_suffix_and_independent_painted_letter_are_not_alias_proof(tmp_path):
    path, main, following = evidence_pdf(tmp_path, mapped="\ufb01")
    assert not confirmed_alias(path, 1, main, "fi", following, "n")
    path.unlink()
    path, main, following = evidence_pdf(tmp_path, text="fin")
    assert not confirmed_alias(path, 1, main, "fi", following, "n")


@pytest.mark.parametrize("change", ["origin", "painted", "font"])
def test_a_suffix_at_another_point_or_with_a_real_glyph_or_font_is_rejected(tmp_path, monkeypatch, change):
    path, main, following = evidence_pdf(tmp_path)
    original = fitz.Page.get_texttrace

    def changed(page):
        spans = copy.deepcopy(original(page))
        chars = list(spans[0]["chars"])
        code, glyph, origin, box = chars[1]
        if change == "origin":
            chars[1] = (code, glyph, (origin[0] + .1, origin[1]), box)
        elif change == "painted":
            chars[1] = (code, 1, origin, box)
        else:
            tail = {**spans[0], "font": "UnrelatedFont", "chars": [chars[1]]}
            spans[0]["chars"] = chars[:1]
            spans.insert(1, tail)
            spans.insert(2, {**spans[0], "chars": chars[2:]})
            return spans
        spans[0]["chars"] = chars
        return spans

    _page_evidence.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(fitz.Page, "get_texttrace", changed)
        assert not confirmed_alias(path, 1, main, "fi", following, "n")
    _page_evidence.cache_clear()


def test_missing_pdf_invalid_pages_and_unembedded_font_cannot_prove_an_alias(tmp_path):
    path, main, following = evidence_pdf(tmp_path)
    for page in (None, True, 0, -1, 99):
        assert not confirmed_alias(path, page, main, "fi", following, "n")
    assert not confirmed_alias(tmp_path / "missing.pdf", 1, main, "fi", following, "n")
    assert not confirmed_alias(None, 1, main, "fi", following, "n")
    with fitz.open() as doc:
        doc.new_page().insert_text((45, 55), "fin")
        path = tmp_path / "unembedded.pdf"
        doc.save(path)
    assert not confirmed_alias(path, 1, main, "fi", following, "n")


def test_proof_cache_is_bound_to_resolved_file_identity_and_page(tmp_path):
    path, main, following = evidence_pdf(tmp_path)
    _page_evidence.cache_clear()
    assert confirmed_alias(path, 1, main, "fi", following, "n")
    before = _page_evidence.cache_info()
    assert confirmed_alias(path.parent / "." / path.name, 1, main, "fi", following, "n")
    assert _page_evidence.cache_info().hits == before.hits + 1
    # Replacing the file at the same path cannot reuse a proof from its old bytes.
    path.write_bytes(_encoded_pdf("abc", {}))
    assert not confirmed_alias(path, 1, main, "fi", following, "n")


def leading_pdf(tmp_path, text="- \ncomple", *, invisible=False, bullet_left=False):
    path = tmp_path / "leading-hyphen.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_font(fontname="embedded", fontbuffer=fitz.Font("cour").buffer)
        for index, line in enumerate(text.split("\n")):
            page.insert_text((90 if bullet_left and index == 0 else 114 if index == 0 else 100, 100 + 14 * index),
                             line, fontname="embedded", fontsize=11, render_mode=3 if invisible else 0)
        raw = page.get_text("rawdict")["blocks"][0]
        first = raw["lines"][0]["spans"][0]["chars"][0]["bbox"]
        second = raw["lines"][1]["spans"][0]["chars"][0]["bbox"] if len(raw["lines"]) > 1 else first
        doc.save(path)
    return path, list(raw["bbox"]), list(first), list(second)


def test_leading_hyphen_requires_the_same_native_block_and_a_painted_first_line(tmp_path):
    path, block, hyphen, following = leading_pdf(tmp_path)
    assert confirmed_leading_hyphen(path, 1, block, hyphen, following, "c")
    assert not confirmed_leading_hyphen(path, 1, [block[0] - 1, *block[1:]], hyphen, following, "c")
    assert not confirmed_leading_hyphen(path, 1, block, hyphen, following, "x")
    assert not confirmed_leading_hyphen(path, 2, block, hyphen, following, "c")
    assert not confirmed_leading_hyphen(None, 1, block, hyphen, following, "c")


@pytest.mark.parametrize(("text", "invisible"), [("body- \ncomple", False), ("- \ncomple", True), ("-comple", False)])
def test_body_prefix_invisible_text_and_same_line_dash_are_not_leading_hyphen_proof(tmp_path, text, invisible):
    path, block, hyphen, following = leading_pdf(tmp_path, text, invisible=invisible)
    assert not confirmed_leading_hyphen(path, 1, block, hyphen, following, "c")


def test_list_bullet_to_the_left_of_its_item_is_not_a_line_end_repair(tmp_path):
    path, block, hyphen, following = leading_pdf(tmp_path, bullet_left=True)
    assert not confirmed_leading_hyphen(path, 1, block, hyphen, following, "c")
