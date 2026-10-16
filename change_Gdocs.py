# ─── google_doc_service.py ───────────────────────────────────────────
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import pathlib
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from typing import Optional
from dataclasses import dataclass


@dataclass
class GoogleDocConfig:
    doc_id: str
    credentials_path: str
    scopes: list[str]


class GoogleDocService:
    def __init__(self, config: GoogleDocConfig):
        self.doc_id = config.doc_id
        self.service = self._build_service(config.credentials_path, config.scopes)


    def _build_service(self, credentials_path: str, scopes: list[str]):
        creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
        return build('docs', 'v1', credentials=creds, cache_discovery=False)

    
    def execute_requests(self, requests: list[dict]):
        self.service.documents().batchUpdate(
            documentId=self.doc_id,
            body={"requests": requests}
        ).execute()


    def get_cell(self, table_idx: int, row: int, col: int) -> dict:
        doc = self.service.documents().get(documentId=self.doc_id).execute()
        table = [e['table'] for e in doc['body']['content'] if 'table' in e][table_idx]
        return table['tableRows'][row]['tableCells'][col]['content'][0]['paragraph']['elements'][0]


    def update_cell_text(self, table_idx: int, row: int, col: int, new_text: str):
        cell = self.get_cell(table_idx, row, col)
        start, end = cell['startIndex'], cell['endIndex']


        requests = [
            {"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}},
            {"insertText": {"location": {"index": start}, "text": new_text}}
        ]

        self.execute_requests([requests])

        print(f"✅ Updated cell [{row},{col}] with: {new_text[:30]}...")
        

    def insert_text_in_cell(self, table_idx: int, row: int, col: int, offset: int, text: str):
        cell = self.get_cell(table_idx, row, col)
        insert_at = cell["startIndex"] + offset

        request = {
            "insertText": {
                "location": {"index": insert_at},
                "text": text
            }
        }

        self.execute_requests([request])


        print(f"✅ Inserted '{text}' at offset {offset} in cell [{row},{col}]")


# ─── block.py ─────────────────────────────────────────────────────────

@dataclass
class EditBlock:
    table_idx: Optional[int] = None
    row: Optional[int] = None
    col: Optional[int] = None
    original_text: Optional[str] = ""
    new_text: Optional[str] = None
    offset: Optional[int] = None
    text_to_insert: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None


    def __post_init__(self):
        # Автоматично оновлює new_text, якщо не задано
        if self.new_text is None:
            self.new_text = self.original_text


    def has_changed(self) -> bool:
        return self.original_text != self.new_text
    
    @classmethod
    def from_insert_json(cls, action: dict):
        return cls(
            offset=action["index"],
            text_to_insert=action["text"]
        )


    def validate(self):
        """
        Перевіряє, чи вказані достатні параметри для виконання.
        Викликається вручну перед .apply().
        """
        by_table_cell = self.table_idx is not None and self.row is not None and self.col is not None
        by_range = self.start is not None and self.end is not None
        by_offset = self.offset is not None
        by_text = self.text_to_insert is not None
        

        if not by_table_cell:
            raise ValueError("❌ Необхідно вказати (table_idx, row, col) для зміни тексту в комірці")
        if not by_range:
            raise ValueError("❌ Необхідно вказати (start, end) для зміни тексту в комірці")
        if not by_offset:
            raise ValueError("❌ Необхідно вказати (table_idx, row, col) зміщення offset")
        if not by_text:
            raise ValueError("❌ Необхідно вказати текст для вставки (text_to_insert)")


# ─── editor.py ────────────────────────────────────────────────────────

class DocEditor:
    def __init__(self, service: GoogleDocService):
        self.service = service


    def update_cell_from_block(self, block: EditBlock):
        if block.has_changed():
            self.service.update_cell_text(
                table_idx=block.table_idx,
                row=block.row,
                col=block.col,
                new_text=block.new_text
            )
            
    def insert_text_for_block(self, block: EditBlock):
        if block.offset is None or not block.text_to_insert:
            raise ValueError("offset and text_to_insert must be provided")
        self.service.insert_text_in_cell(
            table_idx=block.table_idx,
            row=block.row,
            col=block.col,
            offset=block.offset,
            text=block.text_to_insert
        )


    def apply_all(self, blocks: list[EditBlock]):
        for block in blocks:
            if block.text_to_insert is not None:
                self.insert_text_for_block(block)
            else:
                self.update_cell_from_block(block)


    def _make_style_request(self, start: int, end: int, kind: str) -> dict | None:
        text_style: dict = {}
        fields: list[str] = []
        if kind == "bold":
            text_style["bold"] = True
            fields.append("bold")
        elif kind == "unbold":
            text_style["bold"] = False
            fields.append("bold")
        elif kind == "highlight":
            text_style["backgroundColor"] = {
                "color": {"rgbColor": {"red": 0.0, "green": 1.0, "blue": 0.0}}
            }
            fields.append("backgroundColor")
        elif kind.startswith("link:"):
            text_style["link"] = {"url": kind[5:]}
            fields.append("link")
        if not text_style:
            return None
        return {
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": text_style,
                "fields": ",".join(fields)
            }
        }

    def _reset_style_request(self, start: int, end: int) -> dict:
        """Скидає текстовий стиль вставленого тексту до дефолту (без успадкування)."""
        return {
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {
                    "bold": False,
                    "italic": False,
                    "underline": False,
                    "strikethrough": False,
                    "foregroundColor": {},
                    "backgroundColor": {},
                },
                "fields": "bold,italic,underline,strikethrough,foregroundColor,backgroundColor",
            }
        }

    def _apply_style(self, start: int, end: int, kind: str) -> None:
        req = self._make_style_request(start, end, kind)
        if req:
            self.service.execute_requests([req])
            print(f"🎨 styled [{start}, {end}] kind={kind}")

    def _insert_row_with_text(self, table_idx: int, after_row: int, cw_col: int, text: str):
        svc = self.service.service   # raw Google API service (GoogleDocService.service)
        doc_id = self.service.doc_id

        # 1. Знаходимо startIndex таблиці в документі
        doc = svc.documents().get(documentId=doc_id).execute()
        table_els = [e for e in doc["body"]["content"] if "table" in e]
        table_start = table_els[table_idx]["startIndex"]

        # 2. Вставляємо новий рядок нижче after_row
        self.service.execute_requests([{
            "insertTableRow": {
                "tableCellLocation": {
                    "tableStartLocation": {"index": table_start},
                    "rowIndex": after_row,
                    "columnIndex": 0,
                },
                "insertBelow": True,
            }
        }])
        print(f"➕ Inserted new table row after row {after_row}")

        # 3. Отримуємо оновлений документ — знаходимо клітинку COPYWRITER нового рядка
        doc = svc.documents().get(documentId=doc_id).execute()
        table_els = [e for e in doc["body"]["content"] if "table" in e]
        new_row = table_els[table_idx]["table"]["tableRows"][after_row + 1]

        col = 0
        insert_idx = None
        for cell in new_row["tableCells"]:
            if col == cw_col:
                insert_idx = cell["content"][0]["paragraph"]["elements"][0]["startIndex"]
                break
            col += cell.get("tableCellStyle", {}).get("columnSpan", 1)
        if insert_idx is None:
            insert_idx = new_row["tableCells"][0]["content"][0]["paragraph"]["elements"][0]["startIndex"]

        # 4. Вставляємо текст
        self.service.execute_requests([{
            "insertText": {"location": {"index": insert_idx}, "text": text}
        }])
        print(f"✅ Inserted '{text}' into new row at index {insert_idx}")

    def _apply_paragraph_inserts(self, para_actions: list[dict]) -> None:
        """
        Застосовує insert_in_paragraph дії: шукає параграф за текстом у живому документі,
        обчислює абсолютний індекс і застосовує вставки у спадному порядку.
        """
        if not para_actions:
            return

        svc = self.service.service
        doc_id = self.service.doc_id

        # Збираємо всі параграфи документу → text: startIndex
        doc = svc.documents().get(documentId=doc_id).execute()
        # key -> УСІ startIndex-и (а не лише перший): текст заголовка може
        # дублюватися у вчительській колонці та COPYWRITER одночасно.
        para_map: dict[str, list[int]] = {}
        for el in doc["body"]["content"]:
            if "paragraph" in el:
                for pe in el["paragraph"].get("elements", []):
                    t = pe.get("textRun", {}).get("content", "")
                    if t and t != "\n":
                        para_map.setdefault(t.rstrip("\n"), []).append(pe["startIndex"])
            elif "table" in el:
                for tr in el["table"].get("tableRows", []):
                    for tc in tr.get("tableCells", []):
                        for cp in tc.get("content", []):
                            if "paragraph" in cp:
                                for pe in cp["paragraph"].get("elements", []):
                                    t = pe.get("textRun", {}).get("content", "")
                                    if t and t != "\n":
                                        para_map.setdefault(t.rstrip("\n"), []).append(pe["startIndex"])

        def _pick(cands: "list[int] | None", expected) -> "int | None":
            if not cands:
                return None
            if expected is not None:
                # найближче до очікуваного індексу COPYWRITER (з lesson_raw) —
                # відсікає дубль у вчительській колонці
                return min(cands, key=lambda v: abs(v - expected))
            return min(cands)  # стабільно: найперше входження за індексом

        # Обчислюємо абсолютні індекси
        resolved: list[dict] = []
        for act in para_actions:
            para_text = act["paragraph_text"]
            expected = act.get("expected_index")
            cands = para_map.get(para_text)
            if not cands:
                # Спроба часткового пошуку (перші 60 символів)
                prefix = para_text[:60]
                for k, v in para_map.items():
                    if k.startswith(prefix):
                        cands = v
                        break
            start_idx = _pick(cands, expected)
            if start_idx is None:
                print(f"⚠️  THEORIA: параграф не знайдений: {repr(para_text[:60])} | {act.get('_cell_id')}")
                continue
            abs_idx = start_idx + act["char_offset"]
            resolved.append({**act, "index": abs_idx})

        # Сортуємо за спаданням індексу (щоб не зміщувати позиції)
        resolved.sort(key=lambda a: a["index"], reverse=True)

        batch: list[dict] = []
        for act in resolved:
            idx = act["index"]
            text = act["text"]
            batch.append({"insertText": {"location": {"index": idx}, "text": text}})
            batch.append(self._reset_style_request(idx, idx + len(text)))
            print(f"✅ THEORIA inserted '{text[:30]}' at index {idx}")
            for s in act.get("post_styles", []):
                abs_s = idx + s["relative_start"]
                abs_e = idx + s["relative_end"]
                if s["kind"] == "delete_bullet":
                    batch.append({"deleteParagraphBullets": {"range": {"startIndex": abs_s, "endIndex": abs_e}}})
                elif s["kind"] == "create_bullet":
                    batch.append({"createParagraphBullets": {"range": {"startIndex": abs_s, "endIndex": abs_e}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
                else:
                    req = self._make_style_request(abs_s, abs_e, s["kind"])
                    if req:
                        batch.append(req)

        if batch:
            self.service.execute_requests(batch)

    def _apply_cell_appends(self, actions: list[dict]) -> None:
        """Точкова вставка суфікса в клітинку — читає свіжий doc перед КОЖНОЮ дією."""
        for act in actions:
            # Читаємо свіжий стан документа перед кожною вставкою, щоб уникнути
            # зсуву позицій після попередніх insertText у тому ж виклику.
            doc = self.service.service.documents().get(documentId=self.service.doc_id).execute()
            tables = [e for e in doc["body"]["content"] if "table" in e]

            t_idx, r_idx, col_idx = act["table"], act["row"], act["col"]
            table = tables[t_idx]["table"]
            header_cells = table["tableRows"][0]["tableCells"]
            skip = 1 if sum(c.get("tableCellStyle", {}).get("columnSpan", 1) for c in header_cells) >= 6 else 0
            col_start = sum(c.get("tableCellStyle", {}).get("columnSpan", 1) for c in header_cells[:skip])

            physical = None
            logical = col_start
            for i, tc in enumerate(table["tableRows"][r_idx]["tableCells"][skip:], start=skip):
                if logical == col_idx:
                    physical = i
                    break
                logical += tc.get("tableCellStyle", {}).get("columnSpan", 1)

            if physical is None:
                print(f"  ❌ append_to_cell: col {col_idx} не знайдено в рядку {r_idx}")
                continue

            cell_content = table["tableRows"][r_idx]["tableCells"][physical].get("content", [])
            last_par = cell_content[-1]
            insert_idx = last_par["endIndex"] - 1  # перед фінальним \n клітинки
            text = act["text"]

            batch = [{"insertText": {"location": {"index": insert_idx}, "text": text}}]
            batch.append(self._reset_style_request(insert_idx, insert_idx + len(text)))
            batch.append({"deleteParagraphBullets": {
                "range": {"startIndex": insert_idx, "endIndex": insert_idx + len(text)}
            }})
            self.service.execute_requests(batch)
            print(f"✅ append_to_cell: inserted at fresh idx {insert_idx} | {repr(text[:40])}")
            print(f"🧹 reset style [{insert_idx}, {insert_idx + len(text)})")

    def apply_inserts_by_index(self, actions: list[dict]):
        # Розділяємо параграф-рівневі дії (THEORIA) від решти
        para_actions    = [a for a in actions if a["action"] == "insert_in_paragraph"]
        append_actions  = [a for a in actions if a["action"] == "append_to_cell"]
        index_actions   = [a for a in actions if a["action"] not in {"insert_in_paragraph", "append_to_cell"}]

        batch: list[dict] = []

        def _flush():
            if batch:
                self.service.execute_requests(batch)
                batch.clear()

        for act in index_actions:
            if act["action"] == "insert_table_row":
                # Потребує проміжного documents().get() — надсилаємо batch перед ним
                _flush()
                self._insert_row_with_text(
                    table_idx=act["table_idx"],
                    after_row=act["after_row"],
                    cw_col=act["cw_col"],
                    text=act["text"],
                )
            elif act["action"] == "insert_text":
                idx  = act["index"]
                text = act["text"]
                batch.append({
                    "insertText": {
                        "location": {"index": idx},
                        "text": text
                    }
                })
                print(f"✅ inserted '{text[:30]}' at index {idx}")
                # Скидаємо успадкований стиль (bold/highlight/color) до дефолту
                batch.append(self._reset_style_request(idx, idx + len(text)))
                print(f"🧹 reset style [{idx}, {idx + len(text)})")
                post_styles = act.get("post_styles", [])
                has_create_bullet = any(s.get("kind") == "create_bullet" for s in post_styles)
                # Знімаємо список з усього вставленого блоку, якщо post_styles
                # не містять явного create_bullet — інакше параграф успадковує
                # bullet-стиль від порожнього рядка що залишився після delete_range.
                if not has_create_bullet:
                    batch.append({"deleteParagraphBullets": {
                        "range": {"startIndex": idx, "endIndex": idx + len(text)}
                    }})
                    print(f"🔧 cleared bullets [{idx}, {idx + len(text)})")
                for s in post_styles:
                    abs_s = idx + s["relative_start"]
                    abs_e = idx + s["relative_end"]
                    if s["kind"] == "delete_bullet":
                        batch.append({"deleteParagraphBullets": {"range": {"startIndex": abs_s, "endIndex": abs_e}}})
                        print(f"🔧 removed bullets [{abs_s}, {abs_e})")
                    elif s["kind"] == "create_bullet":
                        batch.append({"createParagraphBullets": {"range": {"startIndex": abs_s, "endIndex": abs_e}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
                        print(f"🔵 created bullets [{abs_s}, {abs_e})")
                    else:
                        req = self._make_style_request(abs_s, abs_e, s["kind"])
                        if req:
                            batch.append(req)
                            print(f"🎨 styled [{abs_s}, {abs_e}] kind={s['kind']}")
            elif act["action"] == "delete_range":
                if act["end_index"] <= act["index"]:
                    print(f"⚠️  SKIP invalid delete_range [{act['index']}, {act['end_index']}) | {act.get('_cell_id')}")
                    continue
                print(f"🗑️  deleting [{act['index']}, {act['end_index']}) | {act.get('_cell_id')}")
                batch.append({
                    "deleteContentRange": {
                        "range": {"startIndex": act["index"], "endIndex": act["end_index"]}
                    }
                })
            elif act["action"] == "format_text":
                if act.get("end_index", 0) <= act["index"]:
                    print(f"⚠️  SKIP invalid format_text [{act['index']}, {act.get('end_index')}) | {act.get('_cell_id')}")
                    continue
                req = self._make_style_request(act["index"], act["end_index"], act["kind"])
                if req:
                    batch.append(req)

        _flush()

        # THEORIA: параграф-рівневі вставки — після застосування всіх інших змін
        if para_actions:
            self._apply_paragraph_inserts(para_actions)

        # Точкові вставки в клітинки (append_to_cell) — свіжі позиції після всіх змін
        if append_actions:
            self._apply_cell_appends(append_actions)


# ─── main.py ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    _info = json.loads((pathlib.Path(__file__).parent / "info.json").read_text(encoding="utf-8"))
    config = GoogleDocConfig(
        doc_id=_info["document_id"],
        credentials_path=_info["credentials"],
        scopes=['https://www.googleapis.com/auth/documents']
    )

    service = GoogleDocService(config)
    editor = DocEditor(service)

    
    input_path = pathlib.Path(__file__).parent / "json_data" / "insert_actions.json"
    with open(input_path, "r", encoding="utf-8") as f:
        actions = json.load(f)
        
    # Спадний індекс; insert_table_row (index=999_999_999) — завжди перший;
    # при рівному індексі — delete_range перед insert_text
    def _sort_key(a):
        return (a.get("index", 0), 1 if a["action"] == "delete_range" else 0)
    actions = sorted(actions, key=_sort_key, reverse=True)

    editor.apply_inserts_by_index(actions)
    
    
    


#     # Отримуємо індекс вставки на початок комірки

#     requests = [
#     ]


# # ▶️ Приклад запуску
