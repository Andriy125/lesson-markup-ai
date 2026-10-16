# build_cells.py  ──────────────────────────────────────────────────────────
"""
1. Читає lesson_raw.json (масив об’єктів kind == "table_cell").
2. Нормалізує назву розділу через UNIFIED_RENAME_MAPPING  → GPT-тег.
3. Групує комірки по (row, section) і складає:
      ### <TAG>
      ### COPYWRITER …
      ### DESIGNER  …
      ### CELL_END
4. Виводить кожний блок окремим .txt або у stdout.
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json, pathlib, textwrap, argparse, re
from collections import defaultdict

_INFO = json.loads((pathlib.Path(__file__).parent / "info.json").read_text(encoding="utf-8"))
DEFAULT_MODULE = _INFO["module"]

# --- 1. аргументи командного рядка ---------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("module", nargs="?", default=DEFAULT_MODULE,
                    help=f"Назва модуля (папка в Text/). Default: {DEFAULT_MODULE}")
args = parser.parse_args()
MODULE = args.module

# --- 2. зчитування JSON ---------------------------------------------------
ROOT     = pathlib.Path(__file__).parent
RAW_FILE = ROOT / "lesson_raw.json"
cells = json.loads(RAW_FILE.read_text(encoding="utf-8"))

NOTES_FILE = ROOT / "Text" / MODULE / "image_notes.json"
image_notes: dict[str, dict] = {}
if NOTES_FILE.exists():
    image_notes = json.loads(NOTES_FILE.read_text(encoding="utf-8"))

# --- 3. збираємо по (row, TAG) -------------------------------------------
blocks = defaultdict(lambda: {"copy": [], "design": [], "integrator": []})

# Лічильники для нумерованих списків: (row_key, listId) → поточний номер
_list_counters: dict[tuple, int] = {}

def _format_list_item(text: str, list_info: dict | None, row_key: str) -> str:
    """Додає маркер списку до тексту якщо потрібно."""
    if not list_info:
        return text
    list_id = list_info.get("listId", "")
    counter_key = (row_key, list_id)
    if list_info["type"] == "numbered":
        _list_counters[counter_key] = _list_counters.get(counter_key, 0) + 1
        return f"{_list_counters[counter_key]}. {text}"
    else:  # bullet
        return f"• {text}"


def _apply_color_tags(text: str, cell: dict) -> str:
    """Обгортає кольоровий текст у теги.

    Два рівні:
    • cell-level — c["bg_color"] == COLOR → вся клітинка
    • run-level  — окремі runs мають attrs["bg_color"] == COLOR

    Кольори:
    • #FFFF00 (yellow)  → <yellow>...</yellow>  — звичайний текст
    • #00FFFF (cyan)    → <cyan>...</cyan>       — текст персонажу (йде в картинки, AI ігнорує)
    """
    COLOR_TAGS = {
        "#FFFF00": "yellow",
        "#00FFFF": "cyan",
    }

    # 1. Ціла клітинка одного кольору
    bg = (cell.get("bg_color") or "").upper()
    if bg in COLOR_TAGS:
        tag = COLOR_TAGS[bg]
        return f"<{tag}>{text}</{tag}>"

    # 2. Часткові runs — реконструюємо текст з тегами
    runs = cell.get("runs") or []
    colored_runs = [
        (r, COLOR_TAGS[r["attrs"]["bg_color"].upper()])
        for r in runs
        if (r.get("attrs", {}).get("bg_color") or "").upper() in COLOR_TAGS
    ]
    if not colored_runs:
        return text

    # Вставляємо теги з кінця щоб не зміщувати offsets
    for run, tag in sorted(colored_runs, key=lambda x: x[0]["offset"], reverse=True):
        start = run["offset"]
        end   = run["offset"] + run["length"]
        text  = text[:start] + f"<{tag}>" + text[start:end] + f"</{tag}>" + text[end:]

    return text


for c in (cell for cell in cells if cell["kind"] == "table_cell"):
    section_col = c.get("section")           # вже може бути "### THEORIA"
    raw_sec = section_col.replace("###", "").strip() if section_col else None
    tag = raw_sec

    row_key = f"{c['table']}_{c['row']}_{tag}"
    if c["column_name"] == "### COPYWRITER":
        text = c["text"].replace("\\n", "\n")
        text = _format_list_item(text, c.get("list"), row_key)
        text = _apply_color_tags(text, c)
        # Для TEST: позначаємо правильну відповідь маркером [✓]
        # c.get("list") відрізняє варіанти відповідей (numbered list) від тексту питання (list=None)
        if tag == "TEST" and c.get("list") and (c.get("bold") or (c.get("bg_color") or "").upper() == "#00FF00"):
            text = text + " [✓]"
        # Для THEORIA/FACT/EXAMPLES: позначаємо bold-заголовки для GPT
        if tag in {"THEORIA", "FACT", "EXAMPLES"} and c.get("bold"):
            text = "[bold] " + text
        blocks[row_key]["copy"].append(text)
    elif c["column_name"] == "### DESIGNER":
        if c["text"].strip():
            blocks[row_key]["design"].append(c["text"])
        # rich_link → URL + нотатки з image_notes.json
        if c.get("rich_links"):
            for rl in c["rich_links"]:
                line = f"{rl['title']}: {rl['url']}"
                note = image_notes.get(rl["url"])
                if note:
                    if note.get("description"):
                        line += f"\n[description: {note['description']}]"
                    if note.get("text_on_image"):
                        line += f"\n[text_on_image: {note['text_on_image']}]"
                blocks[row_key]["design"].append(line)
        # inline object → URI + нотатки
        if c.get("inline_objects"):
            for io in c["inline_objects"]:
                line = f"[inline image: {io['object_id']}]"
                note = image_notes.get(io["object_id"])
                if note:
                    if note.get("description"):
                        line += f"\n[description: {note['description']}]"
                    if note.get("text_on_image"):
                        line += f"\n[text_on_image: {note['text_on_image']}]"
                blocks[row_key]["design"].append(line)
    elif c["column_name"] == "### INTEGRATOR":
        if c["text"].strip():
            blocks[row_key]["integrator"].append(c["text"])


# --- 4. helper для форматування ------------------------------------------
def emit_block(tag: str, copy: str, designer: str, integrator: str, cell_id: str) -> str:
    copy_clean = textwrap.dedent(copy).strip()
    copy_marked = copy_clean.replace("\n", "⏎\n")  # ⏎ = видимий кінець рядка для Gemini; стрипається у cells_to_actions.py
    design_clean = textwrap.dedent(designer).strip()
    integrator_clean = textwrap.dedent(integrator).strip()

    return (
        "### CELL_START\n"
        f"CELL_ID: {cell_id}\n"
        f"SECTION: {tag}\n\n"
        "### COPYWRITER\n"
        f"{copy_marked}\n\n"
        "### DESIGNER\n"
        f"{design_clean}\n\n"
        "### INTEGRATOR\n"
        f"{integrator_clean}\n\n"
        "### CELL_END\n"
    )

# --- 5. пишемо у файли / stdout ------------------------------------------
out_dir = ROOT / "Text" / MODULE / "cells_out"
out_dir.mkdir(parents=True, exist_ok=True)

# Очищаємо stale файли з попередніх запусків
_stale = [f for f in out_dir.glob("*.txt")]
if _stale:
    for _f in _stale:
        _f.unlink()
    print(f"🗑️  Видалено {len(_stale)} stale файлів з {out_dir}/")

_QUESTION_START_RE = re.compile(r'^\d+[\.\)]')

def _join_copy(paragraphs: list[str], tag: str) -> str:
    if tag != "TEST":
        return "\n".join(paragraphs)
    # Для TEST — порожній рядок перед кожним новим запитанням (^\d+.)
    result: list[str] = []
    for p in paragraphs:
        if result and _QUESTION_START_RE.match(p.strip()):
            result.append("")
        result.append(p)
    return "\n".join(result)


for key, data in blocks.items():
    tag = key.split("_")[-1]
    cell_txt = emit_block(
        tag,
        _join_copy(data["copy"], tag),
        "\n".join(data["design"]),
        "\n".join(data["integrator"]),
        cell_id=key
    )
    (out_dir / f"{key}.txt").write_text(cell_txt, encoding="utf-8")

print(f"✅  {len(blocks)} cell-payloads готові у  {out_dir}/")
