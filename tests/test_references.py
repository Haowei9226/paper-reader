import pymupdf as fitz
import pytest
from urllib.parse import parse_qs, urlsplit

from reader.pdf import slice_text
from reader.references import _external_target, _link_library_entries

from .conftest import import_and_parse, sample_pdf


def citation_pdf():
    with fitz.open() as doc:
        first = doc.new_page(width=610, height=792)
        first.insert_text((54, 58), "Citation navigation fixture", fontsize=16)
        first.insert_text((54, 133), "This claim uses several works [1, 3-4]. Another claim uses [2].", fontsize=12)
        first.insert_text((54, 646), "References", fontsize=15)
        first.insert_text((54, 677), "[1] Ada Baker. 2021. A study with a title", fontsize=11)
        second = doc.new_page(width=610, height=792)
        second.insert_text((54, 30), "Citation navigation fixture", fontsize=9)
        second.insert_text((60, 101), "continued on another page. https:  //doi.org/10.", fontsize=11)
        second.insert_text((60, 115), "1234/ref-example.", fontsize=11)
        second.insert_text((54, 131), "[2] Bo Chen. 2022. Another title discusses [42] as data.", fontsize=11)
        second.insert_text((54, 145), "[3] C. Diaz. 2023. A third reference.", fontsize=11)
        second.insert_text((54, 159), "[4] D. Evans. 2024. The last reference.", fontsize=11)
        second.insert_text((54, 222), "Appendix A", fontsize=15)
        second.insert_text((54, 250), "[1] An appendix example, unrelated to the bibliography.", fontsize=11)
        doc.set_metadata({"title": "Citation navigation fixture"})
        return doc.tobytes()


def test_references_join_pages_and_preserve_exact_number_anchors(client):
    paper = import_and_parse(client, citation_pdf())
    pid = paper["id"]
    response = client.get(f"/api/papers/{pid}/references")
    assert response.status_code == 200
    index = response.json()
    assert index["heading"]["page"] == 1 and index["heading"]["quote"] == "References"
    entries = index["entries"]
    assert [entry["number"] for entry in entries] == [1, 2, 3, 4]
    assert "A study with a title continued on another page." in entries[0]["text"]
    assert entries[0]["url"] == "https://doi.org/10.1234/ref-example"
    assert "Citation navigation fixture" not in entries[0]["text"]
    assert "[42] as data" in entries[1]["text"] and "appendix example" not in entries[-1]["text"]
    for entry in entries:
        anchor = entry["anchor"]
        page = client.get(f"/api/papers/{pid}/pages/{anchor['page']}?geometry=false").json()
        source = next(block["text"] for block in page["blocks"] if block["id"] == anchor["block_id"])
        assert slice_text(source, anchor["start"], anchor["end"]) == f'[{entry["number"]}]'


def test_unnumbered_section_exposes_author_year_without_guessing_printed_numbers(client):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((54, 56), "Unnumbered references fixture", fontsize=16)
        page.insert_text((54, 164), "Works Cited", fontsize=14)
        page.insert_text((54, 192), "Lee, A. (2021). An unnumbered reference.", fontsize=11)
        doc.set_metadata({"title": "Unnumbered references fixture"})
        paper = import_and_parse(client, doc.tobytes())
    index = client.get(f'/api/papers/{paper["id"]}/references').json()
    assert index["heading"]["quote"] == "Works Cited"
    entry, = index["entries"]
    assert entry["number"] < 0 and entry["numbered"] is False
    assert entry["display_label"] == "Lee, 2021"
    assert {"author": "lee", "year": "2021"} in entry["citation_keys"]


def test_references_link_only_unique_active_local_dois(client):
    source = import_and_parse(client, citation_pdf())["id"]
    target = import_and_parse(client, sample_pdf(pages=1))
    duplicate = import_and_parse(client, sample_pdf(pages=2))["id"]

    def match():
        return client.get(f"/api/papers/{source}/references").json()["entries"][0]["library_match"]

    assert match() is None
    assert client.patch(f"/api/papers/{target['id']}", json={"doi": "https://doi.org/10.1234%2FREF-Example"}).is_success
    assert match() == {"paper_id": target["id"], "title": target["title"]}
    assert client.patch(f"/api/papers/{duplicate}", json={"doi": "doi:10.1234/ref-example"}).is_success
    assert match() is None
    assert client.delete(f"/api/papers/{duplicate}").is_success
    assert match()["paper_id"] == target["id"]
    assert client.patch(f"/api/papers/{source}", json={"doi": "10.1234/ref-example"}).is_success
    assert match() is None


def link_reference(client, source, text):
    from reader.metadata import doi_name

    doi = doi_name(text)
    url, kind = _external_target(text, doi)
    entries = [{"text": text, "doi": doi, "url": url, "url_kind": kind}]
    with client.store.connect() as db:
        _link_library_entries(db, client.store.paper(source, db), entries)
    return entries[0]


@pytest.mark.parametrize("reference", [
    "Alice Ng. 2024. Shared Representations in Collaborative Reading. Journal of Reading.",
    "Ng, A. (2024a). Shared Representations in Collaborative Reading. Journal of Reading.",
    'A. Ng and B. Chen, "Shared Representations in Collaborative Reading," Journal of Reading, 2024.',
    "Ng et al. 2024. Shared Representations in Collaborative Reading. Journal of Reading.",
])
def test_references_match_exact_title_with_first_author_and_year(client, reference):
    source = import_and_parse(client, citation_pdf())["id"]
    target = import_and_parse(client, sample_pdf(pages=1))
    client.patch(f"/api/papers/{target['id']}", json={"authors": "Alice Ng; Bo Chen", "year": "2024", "doi": "10.1234/local"})
    result = link_reference(client, source, reference)
    assert result["library_match"] == {"paper_id": target["id"], "title": target["title"]}
    assert result["library_match_method"] == "title_author_year"
    assert result["url"] == "https://doi.org/10.1234/local" and result["url_kind"] == "doi"


@pytest.mark.parametrize("reference", [
    "Another Ng. 2024. Shared Representations in Collaborative Reading. Journal of Reading.",
    "Alice Ng. 2023. Shared Representations in Collaborative Reading. Journal of Reading.",
    "Alice Ng. 2024. Shared Representations in Collaborative Readings. Journal of Reading.",
    "Alice Ng. 2024. Shared Representations in Collaborative Reading: A Review. Journal of Reading.",
    "Alice Ng. 2024. Using Shared Representations in Collaborative Reading. Journal of Reading.",
    "2024. Shared Representations in Collaborative Reading. Alice Ng.",
    "Alice Ng. Shared Representations in Collaborative Reading. Journal of Reading.",
    "Alice Ng. 2024. Shared Representations in Collaborative Reading. doi:10.1234/another-work",
])
def test_references_reject_partial_titles_wrong_bibliography_and_doi_conflicts(client, reference):
    source = import_and_parse(client, citation_pdf())["id"]
    target = import_and_parse(client, sample_pdf(pages=1))
    client.patch(f"/api/papers/{target['id']}", json={"authors": "Alice Ng", "year": "2024", "doi": "10.1234/local"})
    result = link_reference(client, source, reference)
    assert result["library_match"] is None and result["library_match_method"] is None


def test_doi_free_matches_remain_unique_and_exclude_self_trash_and_short_titles(client):
    source = import_and_parse(client, citation_pdf())["id"]
    first = import_and_parse(client, sample_pdf(pages=1))
    second = import_and_parse(client, sample_pdf(pages=2))
    fields = {"authors": "Alice Ng", "year": "2024", "title": first["title"]}
    for paper in (first, second):
        client.patch(f"/api/papers/{paper['id']}", json=fields)
    reference = f"Alice Ng. 2024. {first['title']}. Journal of Reading."
    assert link_reference(client, source, reference)["library_match"] is None
    client.delete(f"/api/papers/{second['id']}")
    assert link_reference(client, source, reference)["library_match"]["paper_id"] == first["id"]
    client.patch(f"/api/papers/{source}", json=fields)
    assert link_reference(client, source, reference)["library_match"] is None
    client.patch(f"/api/papers/{first['id']}", json={"title": "Reading"})
    assert link_reference(client, source, "Alice Ng. 2024. Reading. Journal.")["library_match"] is None


def test_doi_free_match_does_not_link_another_copy_of_the_source_work(client):
    source = import_and_parse(client, citation_pdf())["id"]
    target = import_and_parse(client, sample_pdf(pages=1))
    fields = {"authors": "Alice Ng", "year": "2024", "doi": "10.1234/same-work"}
    client.patch(f"/api/papers/{source}", json=fields)
    client.patch(f"/api/papers/{target['id']}", json=fields)
    reference = f"Alice Ng. 2024. {target['title']}. Journal of Reading."
    assert link_reference(client, source, reference)["library_match"] is None


def test_chinese_reference_and_matched_source_url(client):
    source = import_and_parse(client, citation_pdf())["id"]
    target = import_and_parse(client, sample_pdf(pages=1))
    fields = {"title": "面向协作阅读的共享表征研究", "authors": "张三; 李四", "year": "2024", "source": "https://example.org/paper"}
    client.patch(f"/api/papers/{target['id']}", json=fields)
    result = link_reference(client, source, "张三等. 面向协作阅读的共享表征研究[J]. 软件学报, 2024.")
    assert result["library_match"] == {"paper_id": target["id"], "title": fields["title"]}
    assert result["url"] == fields["source"] and result["url_kind"] == "url"


@pytest.mark.parametrize(("text", "kind", "expected"), [
    ("An article. https://example.org/papers/work.html.", "url", "https://example.org/papers/work.html"),
    ("Preprint. arXiv:1706.03762v2.", "url", "https://arxiv.org/abs/1706.03762v2"),
    ("Preprint. arXiv:cs/9901001.", "url", "https://arxiv.org/abs/cs/9901001"),
    ("arXiv:2501.12345. https://arxiv.org/abs/2501.12345v2", "url", "https://arxiv.org/abs/2501.12345v2"),
    ("Preprint. ArXiv abs/2305.11473 (2023). https://api.semanticscholar.org/CorpusID: 258823121", "url", "https://arxiv.org/abs/2305.11473"),
    ("https://api.semanticscholar.org/CorpusID: 258823121", "url", "https://api.semanticscholar.org/CorpusID:258823121"),
    ("Bad URL https://[invalid/paper", "search", None),
    ("Private URL https://user:password@example.org/paper", "search", None),
    ("Bad URL https://example.org/%0afoo", "search", None),
])
def test_external_reference_targets_are_safe_links_or_explicit_search(text, kind, expected):
    url, found_kind = _external_target(text, "")
    assert found_kind == kind
    if expected:
        assert url == expected
    else:
        assert url.startswith("https://scholar.google.com/scholar?")
        assert parse_qs(urlsplit(url).query)["q"] == [text]


def bibliography_pdf(lines, *, merged=False):
    with fitz.open() as doc:
        page = doc.new_page(width=650, height=900)
        page.insert_text((48, 60), "Reference formats fixture", fontsize=16)
        if merged:
            page.insert_textbox(fitz.Rect(48, 150, 610, 850), "References\n" + "\n".join(lines), fontsize=10, fontname="china-s")
        else:
            page.insert_text((48, 150), "References", fontsize=14)
            for index, line in enumerate(lines):
                page.insert_text((48, 185 + index * 28), line, fontsize=10, fontname="china-s")
        doc.set_metadata({"title": "Reference formats fixture"})
        return doc.tobytes()


def assert_reference_anchors(client, paper_id, index):
    for anchor in [index["heading"], *(entry["anchor"] for entry in index["entries"])]:
        page = client.get(f"/api/papers/{paper_id}/pages/{anchor['page']}").json()
        source = next(block["text"] for block in page["blocks"] if block["id"] == anchor["block_id"])
        assert slice_text(source, anchor["start"], anchor["end"]) == anchor["quote"]


@pytest.mark.parametrize("labels", [
    ("[1]", "[2]"), ("(1)", "(2)"), ("1.", "2."), ("1)", "2)"), ("1", "2"),
    ("［１］", "［２］"), ("【一】", "【二】"), ("（一）", "（二）"), ("一、", "二、"), ("①", "②"),
])
def test_common_numbered_bibliography_labels_preserve_real_numbers_and_anchors(client, labels):
    paper = import_and_parse(client, bibliography_pdf([
        labels[0] + " Ada Baker. 2021. First reference title.",
        labels[1] + " Bo Chen. 2022. Second reference title.",
    ]))
    index = client.get(f"/api/papers/{paper['id']}/references").json()
    assert [entry["number"] for entry in index["entries"]] == [1, 2]
    assert all(entry["numbered"] for entry in index["entries"])
    assert_reference_anchors(client, paper["id"], index)


def test_author_date_formats_and_organizations_share_a_block_with_references_heading(client):
    paper = import_and_parse(client, bibliography_pdf([
        "Smith, A., & Jones, B. (2020a). First title. Journal of Reading.",
        "Smith, A., & Jones, B. 2020b. A second title. Reading Press.",
        "Chen, Alice, and Bob Diaz. 2022. A Chicago author date title. Reading Press.",
        "World Health Organization (WHO). (n.d.). Guidance for reading. https://example.org/guidance",
        "de Vries, A., & van Dijk, B. (2021). A study of teams. Journal.",
        "张三,李四. 面向协作阅读的共享表征研究[J]. 软件学报,2021.",
        "王五等. (2022). 团队协作支持方法. 学术期刊.",
    ], merged=True))
    index = client.get(f"/api/papers/{paper['id']}/references").json()
    entries = index["entries"]
    assert len(entries) == 7 and all(not entry["numbered"] and entry["number"] < 0 for entry in entries)
    expected = [
        ("smithjones", "2020a"), ("smithjones", "2020b"), ("chendiaz", "2022"),
        ("worldhealthorganization", "n.d."), ("devriesvandijk", "2021"), ("张三李四", "2021"), ("王五", "2022"),
    ]
    for entry, (author, year) in zip(entries, expected):
        assert {"author": author, "year": year} in entry["citation_keys"]
        assert entry["display_label"]
    assert {"author": "who", "year": "n.d."} in entries[3]["citation_keys"]
    assert entries[3]["url"] == "https://example.org/guidance"
    assert_reference_anchors(client, paper["id"], index)


def test_multiline_author_byline_is_one_entry_and_existing_library_link_still_works(client):
    target = import_and_parse(client, sample_pdf(pages=1))
    client.patch(f"/api/papers/{target['id']}", json={"authors": "Alice Ng", "year": "2024", "doi": "10.1234/local"})
    paper = import_and_parse(client, bibliography_pdf([
        "Akoury, N.; Wang, S.; Whiting, J.; Hood, S.; Peng, N.; and",
        "Iyyer, M. 2020. A long author list continues onto this line. Journal.",
        "Ng, A. (2024). Shared Representations in Collaborative Reading. Journal.",
    ], merged=True))
    index = client.get(f"/api/papers/{paper['id']}/references").json()
    first, second = index["entries"]
    assert "Iyyer" in first["text"] and {"author": "akoury", "year": "2020"} in first["citation_keys"]
    assert second["library_match"]["paper_id"] == target["id"]
    assert second["url"] == "https://doi.org/10.1234/local"
    assert_reference_anchors(client, paper["id"], index)


def test_numbered_notes_after_bibliography_are_not_more_reference_entries(client):
    paper = import_and_parse(client, bibliography_pdf([
        "Lee, A. (2021). A reference title. Reading Press.",
        "Notes",
        "1 A footnote discusses an unrelated point in 2020.",
    ]))
    index = client.get(f"/api/papers/{paper['id']}/references").json()
    entry, = index["entries"]
    assert not entry["numbered"] and "footnote" not in entry["text"]


def test_numbered_continuations_do_not_become_author_year_entries(client):
    paper = import_and_parse(client, bibliography_pdf([
        "[1] A. Baker. 2021. A study of collaborative reading.",
        "Accessed: 2025-11-24.",
        "Proceedings of the 2021 CHI Conference. Pages 1-15.",
        "In Proceedings of the 2020 CHI Conference. Pages 1-12.",
        "[2] B. Chen. 2022. Another reference title.",
    ]))
    entries = client.get(f"/api/papers/{paper['id']}/references").json()["entries"]
    assert [entry["number"] for entry in entries] == [1, 2]
    assert "Accessed" in entries[0]["text"] and "2020 CHI" in entries[0]["text"]


def test_first_initial_keys_disambiguate_same_surname_same_year(client):
    paper = import_and_parse(client, bibliography_pdf([
        "Lee, A. (2021). First author's paper. Reading Press.",
        "Lee, B. (2021). Second author's paper. Reading Press.",
    ]))
    first, second = client.get(f"/api/papers/{paper['id']}/references").json()["entries"]
    assert {"author": "lee", "year": "2021"} in first["citation_keys"]
    assert {"author": "lee", "year": "2021"} in second["citation_keys"]
    assert {"author": "alee", "year": "2021"} in first["citation_keys"]
    assert {"author": "blee", "year": "2021"} in second["citation_keys"]
    assert first["display_label"] == "A. Lee, 2021" and second["display_label"] == "B. Lee, 2021"


def test_hanging_title_with_a_year_is_a_continuation(client):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((48, 60), "Hanging bibliography fixture", fontsize=16)
        page.insert_text((48, 150), "References", fontsize=14)
        page.insert_text((48, 185), "Baker, A. (2021).", fontsize=11)
        page.insert_text((65, 204), "Artificial Intelligence in 2024: A Survey.", fontsize=11)
        page.insert_text((65, 223), "Future Studies. 2025. Reading Press.", fontsize=11)
        page.insert_text((48, 260), "Ng, A. (2024). Another paper. Journal of Reading.", fontsize=11)
        doc.set_metadata({"title": "Hanging bibliography fixture"})
        paper = import_and_parse(client, doc.tobytes())
    entries = client.get(f"/api/papers/{paper['id']}/references").json()["entries"]
    assert len(entries) == 2
    assert entries[0]["display_label"] == "Baker, 2021"
    assert "Artificial Intelligence in 2024" in entries[0]["text"] and "Future Studies. 2025" in entries[0]["text"]


def test_right_column_preceding_sections_do_not_end_a_lower_left_bibliography(client):
    with fitz.open() as doc:
        page = doc.new_page(width=650, height=900)
        page.insert_text((48, 60), "Two column bibliography fixture", fontsize=16)
        page.insert_text((48, 350), "References", fontsize=14)
        page.insert_text((48, 385), "Lee, A. (2020). A left column reference.", fontsize=10)
        page.insert_text((340, 80), "A declaration preceding the bibliography.", fontsize=10)
        page.insert_text((340, 140), "Publisher's note", fontsize=14)
        page.insert_text((340, 175), "A publisher declaration, outside the references.", fontsize=10)
        page.insert_text((340, 270), "Supplementary material", fontsize=14)
        page.insert_text((340, 305), "See the supplementary files online.", fontsize=10)
        page.insert_text((340, 385), "Baker, B. (2021). A right column reference.", fontsize=10)
        doc.set_metadata({"title": "Two column bibliography fixture"})
        paper = import_and_parse(client, doc.tobytes())
    index = client.get(f"/api/papers/{paper['id']}/references").json()
    assert [entry["display_label"] for entry in index["entries"]] == ["Lee, 2020", "Baker, 2021"]
    assert all("declaration" not in entry["text"] and "supplementary" not in entry["text"] for entry in index["entries"])
    assert_reference_anchors(client, paper["id"], index)
