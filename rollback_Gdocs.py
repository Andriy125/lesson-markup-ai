#!/usr/bin/env python3
"""
rollback_Gdocs.py — відкат змін у Google Docs із rollback_snapshot.json.

USAGE:
    python rollback_Gdocs.py                          # повний відкат
    python rollback_Gdocs.py --sections PRACTICA      # лише PRACTICA
    python rollback_Gdocs.py --sections THEORIA,TEST  # кілька секцій
    python rollback_Gdocs.py --cell 16                # лише рядок 16
    python rollback_Gdocs.py --dry-run                # показати без змін
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json, pathlib, argparse

ROOT          = pathlib.Path(__file__).parent
ROLLBACK_FILE = ROOT / "json_data" / "rollback_snapshot.json"

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


def hex_to_rgb_color(hex_str):
    """#RRGGBB → Docs OptionalColor; None/'' → {} (очищує колір)."""
    if not hex_str:
        return {}
    h = hex_str.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}


def attrs_to_text_style(attrs):
    """attrs (bold/italic/underline/fg_color/bg_color) → (textStyle, fields)."""
    text_style = {}
    fields = []
    if "bold" in attrs:
        text_style["bold"] = bool(attrs["bold"]); fields.append("bold")
    if "italic" in attrs:
        text_style["italic"] = bool(attrs["italic"]); fields.append("italic")
    if "underline" in attrs:
        text_style["underline"] = bool(attrs["underline"]); fields.append("underline")
    if "fg_color" in attrs:
        text_style["foregroundColor"] = hex_to_rgb_color(attrs["fg_color"])
        fields.append("foregroundColor")
    if "bg_color" in attrs:
        text_style["backgroundColor"] = hex_to_rgb_color(attrs["bg_color"])
        fields.append("backgroundColor")
    return text_style, fields


def load_original_cells(blocks):
    """Блоки lesson_raw → {(table,row,col): [paragraph, ...]} (сортовані за start)."""
    index = {}
    for b in blocks:
        if b.get("kind") != "table_cell":
            continue
        key = (b.get("table"), b.get("row"), b.get("col"))
        index.setdefault(key, []).append(b)
    for key in index:
        index[key].sort(key=lambda p: p.get("start", 0))
    return index


def build_cell_text(paragraphs):
    """Оригінальний текст клітинки: параграфи через \\n."""
    return "\n".join(p.get("text", "") or "" for p in paragraphs)


def build_style_requests(cell_start, paragraphs):
    """Стильові запити (updateTextStyle / createParagraphBullets) відносно cell_start."""
    requests = []
    offset = cell_start
    for p in paragraphs:
        text = p.get("text", "") or ""
        ps = offset
        pe = ps + len(text)
        if pe > ps:
            base = {k: p.get(k) for k in
                    ("bold", "italic", "underline", "fg_color", "bg_color")}
            ts, fields = attrs_to_text_style(base)
            if fields:  # порожній fields → Docs API відхилив би запит (400)
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": ps, "endIndex": pe},
                    "textStyle": ts, "fields": ",".join(fields),
                }})

            for run in (p.get("runs") or []):
                rs = ps + run.get("offset", 0)
                re_ = min(rs + run.get("length", 0), pe)
                if re_ > rs:
                    rts, rfields = attrs_to_text_style(run.get("attrs", {}))
                    if rfields:
                        requests.append({"updateTextStyle": {
                            "range": {"startIndex": rs, "endIndex": re_},
                            "textStyle": rts, "fields": ",".join(rfields),
                        }})

            if p.get("link"):
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": ps, "endIndex": pe},
                    "textStyle": {"link": {"url": p["link"]}}, "fields": "link",
                }})

            if p.get("list"):
                preset = ("NUMBERED_DECIMAL_ALPHA_ROMAN"
                          if p["list"].get("type") == "numbered"
                          else "BULLET_DISC_CIRCLE_SQUARE")
                requests.append({"createParagraphBullets": {
                    "range": {"startIndex": ps, "endIndex": pe},
                    "bulletPreset": preset,
                }})

        offset = pe + 1  # хвостовий \n між параграфами
    return requests


def find_table_cell(doc, table_idx, row_idx, col_logical):
    """Знаходить клітинку в живому документі Google Docs за логічним індексом колонки.

    Повертає (cell_start, cell_end, physical_idx) або None, якщо не знайдено.
    Враховує skip_cells/columnSpan-логіку, аналогічну generate_symbol.py та main().
    """
    # Вибираємо всі елементи з таблицями
    table_els = [e for e in doc["body"]["content"] if "table" in e]
    if table_idx >= len(table_els):
        return None
    rows = table_els[table_idx]["table"]["tableRows"]
    if row_idx >= len(rows):
        return None

    # Визначаємо skip та col_start за header-рядком (рядок 0)
    # TODO: generate_symbol.py додатково детектить 'Копірайтер'/'COPYWRITER' у шапці для
    # обчислення skip; тут лишена проста heuristic total_span>=6 (як у старому rollback).
    # Для продакшн-таблиць (7+ колонок з «Копірайтер») збігається; розійдеться лише на
    # нерегулярних шапках. Тоді — синхронізувати з generate_symbol або зберігати col_start у snapshot.
    header_cells = rows[0]["tableCells"]
    total_span = sum(c.get("tableCellStyle", {}).get("columnSpan", 1) for c in header_cells)
    skip = 1 if total_span >= 6 else 0
    col_start = sum(c.get("tableCellStyle", {}).get("columnSpan", 1)
                    for c in header_cells[:skip])

    # Знаходимо фізичний індекс клітинки за логічним col_logical
    cells = rows[row_idx]["tableCells"]
    physical = None
    logical = col_start
    for i, tc in enumerate(cells[skip:], start=skip):
        if logical == col_logical:
            physical = i
            break
        logical += tc.get("tableCellStyle", {}).get("columnSpan", 1)
    if physical is None:
        return None

    # Збираємо всі paragraph-елементи знайденої клітинки
    all_els = []
    for para in cells[physical].get("content", []):
        if "paragraph" in para:
            all_els.extend(para["paragraph"].get("elements", []))
    if not all_els:
        return None

    cell_start = all_els[0]["startIndex"]
    cell_end = all_els[-1]["endIndex"] - 1  # зберігаємо фінальний \n клітинки
    return (cell_start, cell_end, physical)


def restore_cell(svc, doc_id, cell, originals, dry_run):
    """Перебудовує одну клітинку з оригіналу (lesson_raw) по живих позиціях."""
    key = (cell["table"], cell["row"], cell["col"])
    paragraphs = originals.get(key)
    if paragraphs is None:
        print(f"  ⚠️  [{cell['cell_id']}] немає в lesson_raw {key}, пропуск")
        return False

    doc = svc.documents().get(documentId=doc_id).execute()
    located = find_table_cell(doc, cell["table"], cell["row"], cell["col"])
    if located is None:
        print(f"  ⚠️  [{cell['cell_id']}] клітинку {key} не знайдено в документі, пропуск")
        return False
    cell_start, cell_end, _ = located

    # КОНТРАКТ: build_cell_text з'єднує параграфи через "\n", а build_style_requests
    # рахує позиції з тим самим сепаратором (offset = pe + 1). Якщо змінюєш сепаратор
    # в одній функції — синхронно зміни в іншій, інакше стилі ляжуть на зсунуті діапазони.
    text = build_cell_text(paragraphs)
    style_reqs = build_style_requests(cell_start, paragraphs)

    if dry_run:
        print(f"  [dry-run] [{cell['cell_id']}] {cell_start}..{cell_end} "
              f"→ {len(text)} симв, {len(style_reqs)} стилів")
        return True

    text_reqs = []
    if cell_end > cell_start:
        text_reqs.append({"deleteContentRange": {
            "range": {"startIndex": cell_start, "endIndex": cell_end}}})
    if text:
        text_reqs.append({"insertText": {
            "location": {"index": cell_start}, "text": text}})
    if text_reqs:
        svc.documents().batchUpdate(
            documentId=doc_id, body={"requests": text_reqs}).execute()
    if text and style_reqs:
        svc.documents().batchUpdate(
            documentId=doc_id, body={"requests": style_reqs}).execute()

    print(f"  ✅ [{cell['cell_id']}] відновлено ({len(text)} симв, {len(style_reqs)} стилів)")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sections", default=None,
        help="Comma-separated секції для відкату, напр. PRACTICA або THEORIA,TEST. "
             "За замовчуванням — всі.",
    )
    parser.add_argument(
        "--cell", default=None,
        help="Відкотити тільки один рядок таблиці (наприклад, 16).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Показати що буде відкочено, не вносити змін.",
    )
    args = parser.parse_args()

    SECTIONS_FILTER = (
        set(s.strip().upper() for s in args.sections.split(","))
        if args.sections else None
    )
    CELL_ROW_FILTER: str | None = args.cell.strip() if args.cell else None

    # ── Завантаження snapshot ────────────────────────────────────────────────────
    if not ROLLBACK_FILE.exists():
        print(f"❌ {ROLLBACK_FILE.name} не знайдений.")
        print("   Спочатку запусти: python cells_to_actions.py")
        sys.exit(1)

    snapshot = json.loads(ROLLBACK_FILE.read_text(encoding="utf-8"))
    _info = json.loads((ROOT / "info.json").read_text(encoding="utf-8"))

    # Guard: snapshot і lesson_raw мають належати тому самому документу
    snap_doc = snapshot.get("document_id")
    info_doc = _info.get("document_id")
    if not snap_doc:
        print("❌ У rollback_snapshot.json немає document_id — не можу визначити документ.")
        sys.exit(1)
    if info_doc and snap_doc != info_doc:  # snap_doc вже гарантовано не порожній вище
        print("❌ document_id у rollback_snapshot.json != info.json.")
        print("   lesson_raw.json/snapshot, ймовірно, належать іншому уроку.")
        print(f"   snapshot={snap_doc}  info={info_doc}")
        sys.exit(1)

    RAW_FILE = ROOT / "lesson_raw.json"
    if not RAW_FILE.exists():
        print(f"❌ {RAW_FILE.name} не знайдений — немає джерела оригіналу зі стилями.")
        sys.exit(1)
    originals = load_original_cells(json.loads(RAW_FILE.read_text(encoding="utf-8")))

    # ── Google Docs сервіс (мінімальна обгортка) ─────────────────────────────────
    creds = Credentials.from_service_account_file(
        _info["credentials"],
        scopes=["https://www.googleapis.com/auth/documents"],
    )
    svc = build("docs", "v1", credentials=creds, cache_discovery=False)
    DOC_ID = snap_doc

    def execute(requests: list[dict]) -> None:
        svc.documents().batchUpdate(
            documentId=DOC_ID, body={"requests": requests}
        ).execute()

    def fetch_doc():
        return svc.documents().get(documentId=DOC_ID).execute()

    def get_table_els(doc):
        return [e for e in doc["body"]["content"] if "table" in e]

    # ── Фільтруємо клітинки ──────────────────────────────────────────────────────
    cells = snapshot["cells"]
    if SECTIONS_FILTER:
        cells = [c for c in cells if c["section"].upper() in SECTIONS_FILTER]
        print(f"🔍 Секції: {SECTIONS_FILTER} → {len(cells)} клітинок")
    if CELL_ROW_FILTER:
        cells = [c for c in cells if str(c["row"]) == CELL_ROW_FILTER]
        print(f"🔍 Рядок: {CELL_ROW_FILTER} → {len(cells)} клітинок")
    if not SECTIONS_FILTER and not CELL_ROW_FILTER:
        print(f"🔄 Повний відкат: {len(cells)} клітинок")

    if not cells:
        print("ℹ️  Нічого відкочувати.")
        sys.exit(0)

    # ── 1. Видалення вставлених рядків (лише при повному відкаті) ────────────────
    inserted_rows = snapshot.get("inserted_rows", [])
    if inserted_rows and not SECTIONS_FILTER and not CELL_ROW_FILTER:
        print(f"\n🗑️  Вставлені рядки таблиці: {len(inserted_rows)}")
        doc = fetch_doc()
        table_els = get_table_els(doc)

        # Видаляємо від найвищого row до найнижчого, щоб не зміщувати індекси
        for ins in sorted(inserted_rows, key=lambda x: (x["table_idx"], x["after_row"]), reverse=True):
            t_idx   = ins["table_idx"]
            row_del = ins["after_row"] + 1  # вставлений рядок = after_row + 1
            if t_idx >= len(table_els):
                print(f"  ⚠️  table_idx={t_idx} не існує, пропуск")
                continue
            table_start = table_els[t_idx]["startIndex"]
            row_count   = len(table_els[t_idx]["table"]["tableRows"])
            if row_del >= row_count:
                print(f"  ⚠️  row={row_del} >= {row_count}, пропуск")
                continue
            if args.dry_run:
                print(f"  [dry-run] delete row {row_del} in table {t_idx}")
                continue
            execute([{
                "deleteTableRow": {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": table_start},
                        "rowIndex": row_del,
                    }
                }
            }])
            print(f"  ✅ Видалено рядок {row_del} з таблиці {t_idx}")

    # ── 2. Відновлення вмісту клітинок з оригіналу (lesson_raw) ──────────────
    print("\n🔄 Відновлення вмісту клітинок з lesson_raw.json...")
    restored = 0
    for cell in cells:
        if restore_cell(svc, DOC_ID, cell, originals, args.dry_run):
            restored += 1

    if args.dry_run:
        print(f"\n[dry-run] Буде відновлено: {restored}/{len(cells)} клітинок")
    else:
        print(f"\n✅ Відкат завершено: {restored}/{len(cells)} клітинок відновлено")


if __name__ == "__main__":
    main()
