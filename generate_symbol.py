#!/usr/bin/env python3
"""
lesson_pipeline.py  –  Google Docs → JSON exporter (без GPT, без патчів).

USAGE:
    python lesson_pipeline.py                         # бере DOCUMENT_ID
    python lesson_pipeline.py 1AbCdE… --verbose       # інший Doc-ID
    python lesson_pipeline.py <URL>  -o out.json
"""

from __future__ import annotations
import argparse, json, logging, re, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import os, pathlib


# історія
# dict_lesson = {

# # оригінал

# німецька мова

# оригінал


# # Зарубіжна література 

# оригінал


# # Українська мова

# оригінал 


# # пізнаємо природу

# оригінал
# }


def debug_table_matrix(table_block: dict) -> list[list[str]]:
    """
    Повертає таблицю у вигляді вкладеного списку: [[рядок1], [рядок2], ...]
    Для кожної клітинки бере лише основний текст (без стилів).
    """
    matrix = []
    rows = table_block["table"]["tableRows"]

    for row in rows:
        line = []
        for cell in row["tableCells"]:
            cell_text = ""
            for el in cell.get("content", []):
                if "paragraph" in el:
                    cell_text = read_paragraph_text(el["paragraph"]).strip()
                    if cell_text:
                        break
            line.append(cell_text)
        matrix.append(line)

    return matrix


def build_table_presence_matrix(table_block: dict) -> list[list[str]]:
    """
    Повертає матрицю, де:
    • '*' — якщо клітинка непорожня;
    • ' ' — якщо клітинка пуста.
    """
    matrix = []
    rows = table_block["table"]["tableRows"]

    for row in rows:
        line = []
        for cell in row["tableCells"]:
            cell_text = ""
            for el in cell.get("content", []):
                if "paragraph" in el:
                    cell_text = read_paragraph_text(el["paragraph"]).strip()
                    if cell_text:
                        break
            line.append("*" if cell_text else " ")
        matrix.append(line)

    return matrix


def debug_column_span_stats(table_block: dict):
    for r_idx, row in enumerate(table_block["table"]["tableRows"]):
        spans = []
        for cell in row["tableCells"]:
            span = cell.get("tableCellStyle", {}).get("columnSpan", 1)
            spans.append(str(span))
        print(f"Row {r_idx:02}: total cols={sum(map(int, spans))}, spans: {' | '.join(spans)}")


# ──────── конфіг ──────────────────────────────────────────────────────
_INFO            = json.loads((pathlib.Path(__file__).parent / "info.json").read_text(encoding="utf-8"))
CREDENTIALS_FILE = _INFO["credentials"]
DOCUMENT_ID      = _INFO["document_id"]
CURRENT_SUBJECT  = _INFO.get("current_subject", "")
SCOPES           = ["https://www.googleapis.com/auth/documents.readonly"]

URL_RE = re.compile(r"https?://docs\.google\.com/document/d/([-\w]{20,})")
IMG_RE = re.compile(r"\b[\w-]+\.(?:png|jpe?g|gif|webp)\b", re.I)

LOG = logging.getLogger("lesson-read")

# ──────── Google Docs helper ──────────────────────────────────────────
def gdocs_service():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return build("docs", "v1", credentials=creds, cache_discovery=False)

def clean_id(raw: str) -> str:
    m = URL_RE.match(raw)
    return m.group(1) if m else raw.strip()

# ──────── колір → #RRGGBB ─────────────────────────────────────────────
def hex_color(style_color: Optional[dict]) -> Optional[str]:
    if not style_color:
        return None
    rgb = style_color.get("color", {}).get("rgbColor", {})
    r, g, b = [int(255 * rgb.get(k, 0)) for k in ("red", "green", "blue")]
    return f"#{r:02X}{g:02X}{b:02X}"

# ──────── стилі textRun ───────────────────────────────────────────────
def run_attrs(tr: dict) -> dict:
    st = tr.get("textStyle", {})
    return {
        "bold": bool(st.get("bold")),
        "italic": bool(st.get("italic")),
        "underline": bool(st.get("underline")),  # ← Додали це
        "fg_color": hex_color(st.get("foregroundColor")),
        "bg_color": hex_color(st.get("backgroundColor")),
    }


def diff_attrs(base: dict, cur: dict) -> dict:
    return {k: v for k, v in cur.items() if v != base[k]}


def paragraph_base_style(paragraph: dict) -> dict:
    total_len = 0
    bold_len = italic_len = underline_len = 0
    fg_first = bg_first = None

    for el in paragraph.get("elements", []):
        tr = el.get("textRun")
        if not tr:
            continue

        txt = tr.get("content", "") or ""
        attrs = run_attrs(tr)
        length = len(txt)
        total_len += length

        if attrs["bold"]:
            bold_len += length
        if attrs["italic"]:
            italic_len += length
        if attrs["underline"]:
            underline_len += length

        fg_first = fg_first or attrs["fg_color"]
        bg_first = bg_first or attrs["bg_color"]

    return {
        "bold": bold_len == total_len and total_len > 0,
        "italic": italic_len == total_len and total_len > 0,
        "underline": underline_len == total_len and total_len > 0,
        "fg_color": fg_first,
        "bg_color": bg_first,
    }


def extract_runs(paragraph: dict, base_attrs: dict) -> Optional[list]:
    runs_out, offset = [], 0
    for el in paragraph.get("elements", []):
        tr = el.get("textRun")
        if not tr:
            continue
        txt = tr.get("content", "")
        diff = diff_attrs(base_attrs, run_attrs(tr))
        if diff:
            runs_out.append({
                "offset": offset,
                "length": len(txt),
                "text": txt,
                "attrs": diff
            })
        offset += len(txt)
    return runs_out or None

# ──────── списки ──────────────────────────────────────────────────────
def list_marker_info(paragraph: dict, lists_dict: dict) -> Optional[dict]:
    bullet = paragraph.get("bullet")

    if not bullet:
        return None
    if bullet["listId"] == "kix.kadep82y5148":
        pass
    list_id = bullet.get("listId")
    lvl = bullet.get("nestingLevel", 0)
    nesting = lists_dict.get(list_id, {}).get("listProperties", {}).get("nestingLevels", [])
    props = nesting[lvl] if lvl < len(nesting) else {}

    # 🔍 Визначаємо тип: нумерований чи маркований
    glyph_format = props.get("glyphFormat")
    glyph_type = props.get("glyphType")
    glyph_symbol = props.get("glyphSymbol")
    
    
    if glyph_symbol:
        return {
            "type": "bullet",
            "glyph": glyph_type,
            "level": lvl,
            "listId": list_id
        }
    if glyph_type:
        return {
            "type": "numbered",
            "format": glyph_format,
            "level": lvl,
            "listId": list_id
        }

    # Якщо формат не заданий, але bullet присутній — вважаємо маркованим списком


# ──────── текст абзацу ────────────────────────────────────────────────
def read_paragraph_text(paragraph: dict) -> str:
    return "".join(
        el.get("textRun", {}).get("content", "")
        for el in paragraph.get("elements", [])
    ).strip()

def first_paragraph_link(paragraph: dict) -> Optional[str]:
    for el in paragraph.get("elements", []):
        tr = el.get("textRun")
        if not tr:
            continue
        link = tr.get("textStyle", {}).get("link", {})
        if link:
            return link.get("url")
    return None

STABLE_COLUMNS = ["### SOURCE", "### COPYWRITER", "### DESIGNER", "### INTEGRATOR", "### NOTES"]



def extract_inline_object_info(object_id: str, inline_objects: dict) -> Optional[dict]:
    obj = inline_objects.get(object_id, {}).get("inlineObjectProperties", {}).get("embeddedObject", {})
    if not obj:
        return None

    image_properties = obj.get("imageProperties", {})
    
    return {
        "object_id": object_id,
        "uri": image_properties.get("contentUri"),  # Ось тут URI до зображення!
    }


def extract_richLink_from_cell(paragraph: dict) -> Optional[list[dict]]:
    media_items = []


    for elements in paragraph.get("elements", []):
            # Rich Link (наприклад: .png з Google Drive)
        rich = elements.get("richLink")
        if rich:
            props = rich.get("richLinkProperties", {})
            title = props.get("title", "").strip()
            uri = props.get("uri", "").strip()
            if title or uri:
                media_items.append({
                    "type": "rich_link",
                    "title": title,
                    "url": uri,
                    "mime": props.get("mimeType")
                })


    return media_items or None


def extract_inline_object_from_cell(paragraph: dict, inline_objects) -> Optional[list[dict]]:
    """
    Витягує інформацію про вбудовані об'єкти (inline objects) з клітинки таблиці.
    Повертає список словників з інформацією про кожен об'єкт.
    """
    inline_objects_list = []
    for element in paragraph.get("elements", []):
        inline_obj = element.get("inlineObjectElement")
        if not inline_obj:
            continue
        obj_id = inline_obj.get("inlineObjectId")
        if not obj_id:
            continue
        info = extract_inline_object_info(obj_id, inline_objects)
        if info:
            inline_objects_list.append(info)
    return inline_objects_list or None



# 0. список «канонічних» секцій, під які є micro-prompt
CANON_SECTIONS = {
    "привітання": "GREETING",      # (не обробляється GPT, але зберігаємо)
    "повторення": "REVISION",
    "теорія":     "THEORIA",
    "цікавий факт": "FACT",
    "приклади":  "EXAMPLES",
    "практика":  "PRACTICA",
    "інтерактив": "INTERACTIVE",
    "словник":   "DICTIONARY",
    "додатки":   "APPENDIX",
    "тест":      "TEST",
    "реакція":   "REACTION",       # зворотній зв'язок / мейн могнішото (НМ)
    "фінал":     "FINAL",
}

# 1. твій «розширений» → канонічний (UA)
UNIFIED_RENAME_MAPPING = {
    # greeting
    "привітання": "привітання",

    # revision
    "повторення": "повторення", "швидке оновлення": "повторення",
    "мрійко-пригадайко": "повторення",

    # theory
    "теорія": "теорія", "теорія лонгрід": "теорія",
    "теорія презентація": "теорія",
    "територія теорії": "теорія", "секрети мови": "теорія",
    "фактотека": "теорія", "точка зустрічі з цікавим": "теорія",
    "приготуй мозок до пригод!": "теорія",

    # fun-fact
    "інтересанте факт": "цікавий факт", "цікавий факт": "цікавий факт",

    # examples
    "приклади": "приклади", "зрозумій через приклади": "приклади",
    "мова в дії": "приклади",

    # practice
    "практика": "практика", "мудровміння": "практика",
    "кроки до майстерності": "практика", "deutsch minuten": "практика",
    "редакторій": "практика", "літмережа": "практика",

    # interactive
    "інтерактив": "інтерактив", "взаємодійка": "інтерактив",
    "spiel spaß!": "інтерактив", "інтерактивний майданчик": "інтерактив",

    # dictionary / glossary
    "словник": "словник", "ключі до мови": "словник",
    "мовний архів": "словник",
    # НМ10 — "словник (ключі до мові)" з дужками
    "словник (ключі до мові)": "словник", "словник (ключі до мови)": "словник",
    "schlüssel zur sprache": "словник", "мій словник": "словник",
    "вокабуляр": "словник", "глосарій": "словник",
    "mein wortschatz": "словник", "wortschatz": "словник",

    # appendix
    "додатки": "додатки", "інфоскарб": "додатки",
    "andere": "додатки", "мемоскоп": "додатки", "цитатоскоп": "додатки",

    # test
    "тест": "тест", "deutsch test": "тест",

    # reaction / feedback (НМ: "meine möglichkeiten", зворотній зв'язок)
    "реакція": "реакція", "зворотній зв'язок": "реакція",
    "meine möglichkeiten": "реакція",

    # final / reflection
    "фінал": "фінал", "фінішна пряма": "фінал",
    "ось і фініш": "фінал", "емоційний фейс-контроль": "фінал",
    "finale": "фінал", "рефлексія": "фінал",
}

# 2. універсальна функція → повертає “GPT-tag” (THEORIA, PRACTICA …)
def map_section(raw_header: str) -> str:
    key = raw_header.strip().lower()
    ua_canon = UNIFIED_RENAME_MAPPING.get(key, None)
    if not ua_canon:
        raise ValueError(f"Невідомий розділ: {raw_header}")
    gpt_tag = CANON_SECTIONS.get(ua_canon)
    if not gpt_tag:
        raise ValueError(f"Не призначено GPT-tag для: {ua_canon}")
    return gpt_tag   # напр. "PRACTICA"


def enrich_with_html_text(blocks: list[dict]) -> None:
    for block in blocks:
        tag = "p" if block.get("kind") == "paragraph" else "div"
        styles = []
        
        if block.get("fg_color"):
            styles.append(f"color:{block['fg_color']}")
        if block.get("bg_color"):
            styles.append(f"background-color:{block['bg_color']}")
        
        # Обгортання стилями
        text = block.get("text", "")
        if block.get("bold"):
            text = f"<strong>{text}</strong>"
        if block.get("italic"):
            text = f"<em>{text}</em>"
        if block.get("underline"):
            text = f"<u>{text}</u>"
        
        style_attr = f' style="{";".join(styles)}"' if styles else ""
        block["html_text"] = f"<{tag}{style_attr}>{text}</{tag}>"


def _cell_has_content(cell: dict) -> bool:
    """True якщо клітинка має текст, rich_link або inline object."""
    for el in cell.get("content", []):
        for elem in el.get("paragraph", {}).get("elements", []):
            if elem.get("textRun", {}).get("content", "").strip():
                return True
            if elem.get("richLink") or elem.get("inlineObjectElement"):
                return True
    return False


def parse_table_block(table_block: dict, doc: dict, table_index: int = 0, inline_objects: dict = {}) -> list[dict]:
    if __debug__:
        matrix = debug_table_matrix(table_block)
        LOG.debug("\U0001F4CA Таблиця #%d:", table_index)
        for r_idx, row in enumerate(matrix):
            LOG.debug("%2d - │ %s", r_idx, " | ".join(row))

        presence_matrix = build_table_presence_matrix(table_block)
        for r_idx, row in enumerate(presence_matrix):
            prefix = f"{r_idx:<2} - │ "
            print(prefix.replace("\u2502", "|") + " | ".join(row))

    blocks = []
    rows = table_block["table"]["tableRows"]
    if not rows:
        return []

    # 1. Визначаємо структуру ТАБЛИЦІ по її першому рядку (шапці)
    first_row_cells = rows[0]["tableCells"]
    total_span = sum(c.get("tableCellStyle", {}).get("columnSpan", 1) for c in first_row_cells)
    
    # Спробуємо знайти "Копірайтер" або "Дизайнер" у першому рядку, щоб визначити зсув
    header_texts = []
    for c in first_row_cells:
        txt = ""
        for el in c.get("content", []):
            if "paragraph" in el:
                txt = read_paragraph_text(el["paragraph"]).strip()
                if txt: break
        header_texts.append(txt)

    # Якщо "Копірайтер" у 2-й клітинці (індекс 1) -> skip_cells = 1
    # Якщо 6+ колонок загалом -> теж skip_cells = 1
    if "Копірайтер" in header_texts:
        skip_cells = header_texts.index("Копірайтер") - 1
        if skip_cells < 0: skip_cells = 0
    elif "COPYWRITER" in [t.upper() for t in header_texts]:
        skip_cells = [t.upper() for t in header_texts].index("COPYWRITER") - 1
        if skip_cells < 0: skip_cells = 0
    else:
        skip_cells = 1 if total_span >= 6 else 0
    
    col_start = sum(
        c.get("tableCellStyle", {}).get("columnSpan", 1)
        for c in first_row_cells[:skip_cells]
    )

    # Знаходимо фізичний індекс колонки INTEGRATOR — один раз на всю таблицю
    integrator_col_idx = None
    _col, _logical = col_start, 0
    for _cell in first_row_cells[skip_cells:]:
        if _logical < len(STABLE_COLUMNS):
            if STABLE_COLUMNS[_logical] == "### INTEGRATOR":
                integrator_col_idx = _col
            _logical += 1
        _col += _cell.get("tableCellStyle", {}).get("columnSpan", 1)

    current_section = None

    # 2. Обробка всіх рядків за визначеною структурою
    for r_idx, row in enumerate(rows):
        cells = row["tableCells"]
        col = col_start
        cell_texts = []
        column_name_map = {}
        logical_idx = 0
        designer_col_in_row = None
        designer_span_in_row = None

        # Секцію визначаємо лише по перших cells[0..skip_cells] — далі йде контент (INTEGRATOR тощо)
        for _cell_i, _cell in enumerate(cells[:skip_cells + 1]):
            for _el in _cell.get("content", []):
                if "paragraph" in _el:
                    _tn = read_paragraph_text(_el["paragraph"]).strip().lower()
                    _tn_clean = re.sub(r'^розділ\s*\d+\.?\s*', '', _tn).strip()
                    _matched = _tn if _tn in UNIFIED_RENAME_MAPPING else (_tn_clean if _tn_clean in UNIFIED_RENAME_MAPPING else None)
                    if _tn and not _matched and _cell_i < skip_cells:
                        # Перша клітинка має текст, але він невідомий → треба додати до маппінгу
                        _safe = _tn.encode('utf-8').hex()
                        print(f"  [WARN r={r_idx}] UNKNOWN SECTION cells[0] (hex): {_safe}")
                    if _matched:
                        current_section = f"### {map_section(_matched)}"
                    break

        # === 1. Побудова column_name_map для поточного рядка ===
        for cell in cells[skip_cells:]:
            span = cell.get("tableCellStyle", {}).get("columnSpan", 1)
            txt = ""
            for el in cell.get("content", []):
                if "paragraph" in el:
                    txt = read_paragraph_text(el["paragraph"])
                    break
            cell_texts.append(txt)

            if logical_idx < len(STABLE_COLUMNS):
                col_name = STABLE_COLUMNS[logical_idx]
                if _cell_has_content(cell):
                    column_name_map[col] = col_name
                # Трекуємо DESIGNER для точного INTEGRATOR
                if col_name == "### DESIGNER":
                    designer_col_in_row = col
                    designer_span_in_row = span
                logical_idx += 1
            col += span

        print(f"{r_idx:02} skip={skip_cells} cells={len(cells)} - {cell_texts}")

        # INTEGRATOR fallback
        per_row_integrator_col = (
            designer_col_in_row + designer_span_in_row
            if designer_col_in_row is not None
            else integrator_col_idx
        )

        # === 2. Обробка клітинок ===
        col = col_start
        for c_idx, cell in enumerate(cells[skip_cells:]):
            span = cell.get("tableCellStyle", {}).get("columnSpan", 1)
            for el in cell.get("content", []):
                if "paragraph" not in el:
                    continue

                par = el.get("paragraph", {})
                txt = read_paragraph_text(par)
                rich_links = extract_richLink_from_cell(par)
                inline_object = extract_inline_object_from_cell(par, inline_objects)
                
                # Якщо клітинка порожня (або лише назва секції) — пропускаємо контент
                _low_txt = txt.strip().lower()
                # is_section_header (→ викид) лише для першої/SOURCE-колонки. У КОНТЕНТНИХ
                # колонках (COPYWRITER/INTEGRATOR/…) слово-аліас секції — це РЕАЛЬНИЙ контент:
                # підказка H5P «тест»/«практика» в INTEGRATOR, назва «Spiel Spaß!» у SOURCE.
                # Раніше викидало → INTEGRATOR ставав порожнім і Gemini не генерував H5P.
                _col_name = column_name_map.get(col)
                is_section_header = (_low_txt in UNIFIED_RENAME_MAPPING) and (_col_name in (None, "### SOURCE"))
                # Виняток (тільки INTERACTIVE): назва інтерактив-блоку (напр. «Spiel Spaß!»)
                # стоїть у SOURCE замість об'єднаної секційної колонки — зберігаємо як контент.
                if is_section_header and current_section == "### INTERACTIVE" \
                        and UNIFIED_RENAME_MAPPING.get(_low_txt) == "інтерактив":
                    is_section_header = False
                if (not txt.strip() or is_section_header) and not rich_links and not inline_object:
                    # Порожня INTEGRATOR-клітинка — зберігаємо лише першу позицію
                    if col == per_row_integrator_col and "startIndex" in el:
                        blocks.append({
                            "kind": "table_cell",
                            "text": "",
                            "start": el["startIndex"],
                            "end": el["endIndex"],
                            "table": table_index,
                            "row": r_idx,
                            "col": col,
                            "column_name": "### INTEGRATOR",
                            "section": current_section,
                            "bold": False, "italic": False, "underline": False,
                            "fg_color": None, "bg_color": None,
                            "runs": None, "list": None, "link": None,
                            "rich_links": None, "inline_objects": None,
                        })
                    continue

                txt = "*картинка" if IMG_RE.fullmatch(txt) else txt
                base = paragraph_base_style(par)
                runs = extract_runs(par, base)

                blocks.append({
                    "kind": "table_cell",
                    "text": txt,
                    "start": el["startIndex"],
                    "end": el["endIndex"],
                    "table": table_index,
                    "row": r_idx,
                    "col": col,
                    "column_name": column_name_map.get(col),
                    "section": current_section,
                    **base,
                    "runs": runs,
                    "list": list_marker_info(par, doc.get("lists", {})),
                    "link": first_paragraph_link(par),
                    "rich_links": rich_links,
                    "inline_objects": inline_object or None,
                })
            col += span
    return blocks


def parse_paragraph_block(part: dict, doc: dict, inline_objects: dict) -> dict:
    par = part["paragraph"]
    txt = read_paragraph_text(par)
    txt = "*картинка" if IMG_RE.fullmatch(txt) else txt
    base = paragraph_base_style(par)
    runs = extract_runs(par, base)

    return {
        "kind": "paragraph",
        "text": txt,
        "start": part["startIndex"],
        **base,
        "runs": runs,
        "list": list_marker_info(par, doc.get("lists", {})),
    }

def export_raw_blocks(doc_id: str) -> List[Dict[str, Any]]:
    doc = gdocs_service().documents().get(documentId=doc_id).execute()
    inline_objects = doc.get("inlineObjects", {})
    blocks: list[dict] = []
    table_idx = 0

    for part in doc["body"]["content"]:
        if "paragraph" in part:
            blocks.append(parse_paragraph_block(part, doc, inline_objects))
        elif "table" in part:
            blocks.extend(parse_table_block(part, doc, table_index=table_idx, inline_objects=inline_objects))
            table_idx += 1

    return blocks




# ──────── CLI ─────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Google Docs → structured JSON")
    p.add_argument("source", nargs="?", help="Doc ID або URL (опц.)")
    p.add_argument("-o", "--out", default="lesson_raw.json",
                   help="файл JSON (pretty-print)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)7s │ %(message)s")
        
    raw_id = args.source or DOCUMENT_ID
    if not raw_id:
        LOG.error("Не передано ні CLI-ID/URL, ні DOCUMENT_ID у коді.")
        sys.exit(1)

    doc_id = clean_id(raw_id)
    LOG.info("📑 Doc ID: %s", doc_id)

    blocks = export_raw_blocks(doc_id)
    enrich_with_html_text(blocks)
    LOG.info("Зібрано %d блок(ів)", len(blocks))

    out_path = Path(args.out)
    out_path.write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("✅ JSON збережено → %s", out_path.absolute())
    
    
    # Якщо потрібно, можна розкоментувати для повного дампу документа

# ──────── запуск ──────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
