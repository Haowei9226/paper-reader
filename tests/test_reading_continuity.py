from copy import deepcopy

from reader.reading import classify_reading_blocks, page_continuation, reading_runs
from reader.store import dumps
from .conftest import import_and_parse


PAPER = {"title": "A Manuscript", "authors": "ANONYMOUS AUTHOR(S)", "outline": []}


def block(text, y, *, x=40, right=470, height=9, kind="paragraph", page=1, section="1 Introduction"):
    return {"id": f"p{page}-{y}-{x}", "text": text, "bbox": [x, y, right, y + height],
            "kind": kind, "page": page, "section": section, "position": int(y)}


def test_typeset_lines_join_after_colon_abbreviation_and_before_capital_or_citation():
    blocks = [block(text, 100 + index * 13.7) for index, text in enumerate((
        "Every learner's vote counts. Its cost is herding:",
        "voters can follow the majority. Plass et al.",
        "[40] synthesise four foundations into an Integrated",
        "Design Framework (IDF), and note the need for alignment.",
    ))]
    raw = deepcopy(blocks)
    classify_reading_blocks(blocks, PAPER, 1, 792)
    assert reading_runs(blocks) == [blocks]
    assert [(b['id'], b['text'], b['bbox']) for b in blocks] == [(b['id'], b['text'], b['bbox']) for b in raw]


def test_paragraph_indent_list_table_heading_and_smaller_notes_break_runs():
    blocks = [
        block("The first sentence is complete and fills this line.", 100),
        block("A second sentence stays in the same paragraph.", 113.7),
        block("A new indented paragraph starts here and continues", 127.4, x=50),
        block("with its next ordinary line of text.", 141.1),
        block("• A separate item has its own paragraph", 154.8),
        block("and its hanging continuation remains attached.", 168.5, x=49),
        block("• A second item is kept separate.", 182.2),
        block("the table values", 195.9),
        block("following prose should not join across a table.", 209.6),
        block("2 Methods", 223.3, kind="heading"),
        block("Ordinary body prose is independent of the small note", 237),
        block("1 a smaller footnote remains independent.", 250.7, height=6),
    ]
    classify_reading_blocks(blocks, PAPER, 1, 792, table_blocks={blocks[7]['id']})
    assert [len(run) for run in reading_runs(blocks)] == [2, 2, 2, 1, 1, 1, 1, 1]


def test_unlabelled_abstract_is_not_part_of_the_byline():
    blocks = [
        block("A Manuscript", 70, height=18, kind="heading"),
        block("ANONYMOUS AUTHOR(S)", 100, right=190, height=11),
        block("Live-streamed instruction gives learners few ways to act on what they are watching. We present a system in", 125, height=8),
        block("which a group uses the chat to vote on the steps while a language model explains each one.", 137.5, height=8),
        block("Additional Key Words and Phrases: Collaborative learning, Game-based learning, Live-streamed education", 165, height=8),
        block("and audience participation games.", 177.5, height=8),
        block("1 Introduction", 205, kind="heading"),
    ]
    classify_reading_blocks(blocks, PAPER, 1, 792)
    assert [b['reading_role'] for b in blocks] == ['title', 'extra', 'text', 'text', 'extra', 'extra', 'heading']
    assert blocks[3]['continues_previous'] is True


def classified(blocks, page):
    classify_reading_blocks(blocks, PAPER, page, 792)
    return blocks


def test_cross_page_bridge_preserves_the_source_and_rejects_structural_boundaries():
    left = classified([block("An earlier normal line establishes the body margin.", 500),
                       block("These systems constrain what the learner can", 650)], 1)
    right = classified([block("see, without disclosing the solution.", 95, page=2),
                        block("An independent ordinary body line follows here.", 108.7, page=2)], 2)
    raw = deepcopy((left, right))
    bridge = page_continuation(left, right, 595, 595)
    assert bridge and bridge['left'] is left[-1] and bridge['right'] is right[0] and not bridge['join_word']
    assert (left, right) == raw
    for updates in ({'reading_role': 'heading'}, {'reading_role': 'table'}, {'section': '2 Methods'},
                    {'text': '• a separate item'}, {'bbox': [50, 95, 470, 104]}, {'bbox': [40, 95, 470, 101]}):
        candidate = deepcopy(right)
        candidate[0].update(updates)
        assert page_continuation(left, candidate, 595, 595) is None
    indented = deepcopy(left)
    indented[-1]['bbox'][0] = 59
    assert page_continuation(indented, right, 595, 595) is None


def test_cross_page_word_wrap_is_explicit_and_sentence_end_is_not_a_bridge():
    left = classified([block("A body margin line precedes the broken term.", 500),
                       block("A sufficiently long sentence ends in a nego\u00ad", 650)], 1)
    right = classified([block("tiation between the participants.", 95, page=2)], 2)
    assert page_continuation(left, right, 595, 595)['join_word'] is True
    left[-1]['text'] = "The previous page ends with a complete sentence."
    assert page_continuation(left, right, 595, 595) is None


def test_page_api_and_export_keep_source_anchors_for_a_cross_page_paragraph(client):
    paper = import_and_parse(client)
    pid = paper['id']
    source = [block('Earlier body prose establishes the normal body margin.', 500),
              block('These systems constrain what the learner can', 650),
              block('see, without disclosing the solution.', 95, page=2),
              block('An independent ordinary body line follows here.', 108.7, page=2)]
    with client.store.connect() as db:
        db.execute('DELETE FROM blocks WHERE paper_id=?', (pid,))
        for b in source:
            db.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (b['id'], pid, b['page'], b['position'], b['kind'], b['text'], dumps(b['bbox']), None, None, b['section']))
    first = client.get(f'/api/papers/{pid}/pages/1').json()
    second = client.get(f'/api/papers/{pid}/pages/2').json()
    assert first['reading_continuation']['next'] == {
        'block_id': source[2]['id'], 'page': 2, 'current_block_id': source[1]['id'], 'join_word': False,
    }
    assert second['reading_continuation']['previous'] == {
        'block_id': source[1]['id'], 'page': 1, 'current_block_id': source[2]['id'], 'join_word': False,
    }
    exported = client.get(f'/api/papers/{pid}/export/html').text
    import re
    paragraph = next(p for p in re.findall(r'<p class="reading-block reading-joined block-text">(.*?)</p>', exported)
                     if f'id="block-{source[1]["id"]}"' in p)
    assert f'id="block-{source[2]["id"]}"' in paragraph
    assert exported.count('id="page-2"') == 1 and '<span id="page-2"' in paragraph
    for b in source:
        assert exported.count(f'id="block-{b["id"]}"') == 1
    with client.store.connect() as db:
        for b in source:
            row = db.execute('SELECT text,bbox FROM blocks WHERE id=?', (b['id'],)).fetchone()
            assert row['text'] == b['text'] and row['bbox'] == dumps(b['bbox'])
