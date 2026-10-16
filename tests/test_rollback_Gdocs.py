def test_module_imports_without_side_effects():
    import rollback_Gdocs  # noqa: F401
    assert hasattr(rollback_Gdocs, "main")


from rollback_Gdocs import hex_to_rgb_color


def test_hex_none_returns_empty():
    assert hex_to_rgb_color(None) == {}


def test_hex_empty_returns_empty():
    assert hex_to_rgb_color("") == {}


def test_hex_red():
    assert hex_to_rgb_color("#FF0000") == {
        "color": {"rgbColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}
    }


def test_hex_without_hash():
    assert hex_to_rgb_color("00FF00") == {
        "color": {"rgbColor": {"red": 0.0, "green": 1.0, "blue": 0.0}}
    }


from rollback_Gdocs import attrs_to_text_style


def test_attrs_bold_only():
    ts, fields = attrs_to_text_style({"bold": True})
    assert ts == {"bold": True}
    assert fields == ["bold"]


def test_attrs_full_base():
    attrs = {
        "bold": False, "italic": True, "underline": False,
        "fg_color": None, "bg_color": "#00FF00",
    }
    ts, fields = attrs_to_text_style(attrs)
    assert ts["bold"] is False
    assert ts["italic"] is True
    assert ts["underline"] is False
    assert ts["foregroundColor"] == {}
    assert ts["backgroundColor"] == {
        "color": {"rgbColor": {"red": 0.0, "green": 1.0, "blue": 0.0}}
    }
    assert set(fields) == {
        "bold", "italic", "underline", "foregroundColor", "backgroundColor"
    }


def test_attrs_empty():
    ts, fields = attrs_to_text_style({})
    assert ts == {}
    assert fields == []


from rollback_Gdocs import load_original_cells, build_cell_text


def test_load_groups_sorts_and_ignores_paragraphs():
    blocks = [
        {"kind": "table_cell", "table": 0, "row": 9, "col": 3, "start": 120, "text": "друга"},
        {"kind": "table_cell", "table": 0, "row": 9, "col": 3, "start": 100, "text": "перша"},
        {"kind": "paragraph", "text": "поза таблицею"},
        {"kind": "table_cell", "table": 0, "row": 9, "col": 5, "start": 200, "text": "інт"},
    ]
    idx = load_original_cells(blocks)
    assert set(idx.keys()) == {(0, 9, 3), (0, 9, 5)}
    assert [p["text"] for p in idx[(0, 9, 3)]] == ["перша", "друга"]


def test_build_cell_text_joins_with_newline():
    paragraphs = [{"text": "перша"}, {"text": "друга"}, {"text": "третя"}]
    assert build_cell_text(paragraphs) == "перша\nдруга\nтретя"


def test_build_cell_text_empty():
    assert build_cell_text([{"text": ""}]) == ""


from rollback_Gdocs import build_style_requests


def _para(text, **over):
    base = {
        "text": text, "bold": False, "italic": False, "underline": False,
        "fg_color": None, "bg_color": None, "runs": None, "list": None, "link": None,
    }
    base.update(over)
    return base


def test_base_style_spans_whole_paragraph():
    reqs = build_style_requests(100, [_para("Заголовок", bold=True)])
    base = reqs[0]["updateTextStyle"]
    assert base["range"] == {"startIndex": 100, "endIndex": 109}  # len("Заголовок")==9
    assert base["textStyle"]["bold"] is True
    assert set(base["fields"].split(",")) == {
        "bold", "italic", "underline", "foregroundColor", "backgroundColor"
    }


def test_run_override_and_clamp():
    # run length 3 включає хвостовий \n → має клампитись до межі "Hi" (2)
    p = _para("Hi", runs=[{"offset": 0, "length": 3, "attrs": {"bold": True}}])
    reqs = build_style_requests(10, [p])
    run = [
        r["updateTextStyle"] for r in reqs
        if r.get("updateTextStyle", {}).get("fields") == "bold"
    ][0]
    assert run["range"] == {"startIndex": 10, "endIndex": 12}
    assert run["textStyle"] == {"bold": True}


def test_bullet_and_link_emitted():
    p = _para("http://x", list={"type": "bullet"}, link="http://x")
    reqs = build_style_requests(0, [p])
    assert any("createParagraphBullets" in r for r in reqs)
    link_req = [r["updateTextStyle"] for r in reqs
                if r.get("updateTextStyle", {}).get("fields") == "link"][0]
    assert link_req["textStyle"]["link"] == {"url": "http://x"}


def test_numbered_list_preset():
    p = _para("1 пункт", list={"type": "numbered"})
    reqs = build_style_requests(0, [p])
    bullet = [r["createParagraphBullets"] for r in reqs if "createParagraphBullets" in r][0]
    assert bullet["bulletPreset"] == "NUMBERED_DECIMAL_ALPHA_ROMAN"


def test_two_paragraphs_offset_accounts_newline():
    reqs = build_style_requests(0, [_para("ab"), _para("cd")])
    ranges = [r["updateTextStyle"]["range"] for r in reqs if "updateTextStyle" in r]
    # 1-й параграф [0,2), 2-й після \n → [3,5)
    assert ranges[0] == {"startIndex": 0, "endIndex": 2}
    assert ranges[1] == {"startIndex": 3, "endIndex": 5}


def test_empty_paragraph_no_requests_but_advances_offset():
    reqs = build_style_requests(0, [_para(""), _para("x")])
    ranges = [r["updateTextStyle"]["range"] for r in reqs if "updateTextStyle" in r]
    # порожній параграф пропущено, "x" після \n → [1,2)
    assert ranges == [{"startIndex": 1, "endIndex": 2}]


from rollback_Gdocs import find_table_cell


def _el(start, end):
    return {"paragraph": {"elements": [{"startIndex": start, "endIndex": end}]}}


def _doc():
    span1 = {"tableCellStyle": {"columnSpan": 1}}
    header = [{**span1, "content": [_el(1, 2)]} for _ in range(4)]
    row1 = [
        {**span1, "content": [_el(10, 11)]},
        {**span1, "content": [_el(11, 12)]},
        {**span1, "content": [_el(12, 30)]},
        {**span1, "content": [_el(30, 31)]},
    ]
    return {"body": {"content": [
        {"table": {"tableRows": [{"tableCells": header}, {"tableCells": row1}]}}
    ]}}


def test_find_cell_by_logical_col():
    assert find_table_cell(_doc(), 0, 1, 2) == (12, 29, 2)


def test_find_cell_missing_table():
    assert find_table_cell(_doc(), 5, 1, 2) is None


def test_find_cell_missing_col():
    assert find_table_cell(_doc(), 0, 1, 99) is None


def _doc_skip1():
    # 6 колонок у шапці → total_span>=6 → skip=1, col_start=1 (продакшн-шлях)
    span1 = {"tableCellStyle": {"columnSpan": 1}}
    header = [{**span1, "content": [_el(1, 2)]} for _ in range(6)]
    row1 = [
        {**span1, "content": [_el(40, 41)]},   # physical 0 — секція, пропускається
        {**span1, "content": [_el(41, 42)]},   # physical 1 → logical 1
        {**span1, "content": [_el(42, 43)]},   # physical 2 → logical 2
        {**span1, "content": [_el(43, 60)]},   # physical 3 → logical 3
        {**span1, "content": [_el(60, 61)]},   # physical 4 → logical 4
        {**span1, "content": [_el(61, 62)]},   # physical 5 → logical 5
    ]
    return {"body": {"content": [
        {"table": {"tableRows": [{"tableCells": header}, {"tableCells": row1}]}}
    ]}}


def test_find_cell_skip1_logical_col():
    # skip=1, col_start=1; col_logical=3 → фізичний індекс 3, межі (43, 59)
    assert find_table_cell(_doc_skip1(), 0, 1, 3) == (43, 59, 3)
