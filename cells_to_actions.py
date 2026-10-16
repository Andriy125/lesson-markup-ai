#!/usr/bin/env python3
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
"""
cells_to_actions.py ─────────────────────────────────────────────────────────
Парсить cells_gpt_out/*.txt, знаходить [[INSERT:xxx]] маркери,
обчислює їхні абсолютні індекси у Google Doc і записує insert_actions.json.

Після цього запускай change_Gdocs.py, який читає insert_actions.json
і вставляє символи у документ.

USAGE:
    python cells_to_actions.py               # DEFAULT_MODULE
    python cells_to_actions.py "ЗЛ6 25"     # конкретний модуль
    python cells_to_actions.py --dry-run     # тільки надрукувати, не писати файл
    python cells_to_actions.py --cell 16               # тільки рядок 16 (абсолютний)
    python cells_to_actions.py --cell PRACTICA:2       # 2-га клітинка PRACTICA
"""

import json, re, argparse, pathlib

_INFO = json.loads((pathlib.Path(__file__).parent / "info.json").read_text(encoding="utf-8"))
DEFAULT_MODULE = _INFO["module"]

parser = argparse.ArgumentParser()
parser.add_argument("module", nargs="?", default=DEFAULT_MODULE,
                    help=f"Назва модуля (папка в Text/). Default: {DEFAULT_MODULE}")
parser.add_argument("--dry-run", action="store_true",
                    help="Не писати файл, тільки вивести результат")
parser.add_argument("--restyle-test", action="store_true",
                    help="Тільки стилізація TEST: bold+highlight для правильних відповідей. Не замінює текст.")
parser.add_argument("--sections", default=None,
                    help="Comma-separated секції, напр. PRACTICA,APPENDIX. За замовчуванням — всі.")
parser.add_argument("--cell", default=None,
                    help="Тільки одна клітинка: рядок (напр. 16) або СЕКЦІЯ:N (напр. PRACTICA:2)")
args = parser.parse_args()
MODULE = args.module
SECTIONS_FILTER = set(s.strip().upper() for s in args.sections.split(",")) if args.sections else None

CURRENT_SUBJECT = _INFO.get("current_subject", "")
# Де лежить домашнє завдання залежить від предмета:
#   • німецька  — у FINAL («Hausaufgaben:», «Lehrbuch s. NN») → пас §7.5
#   • українська — у APPENDIX («Домашнє завдання:», «с. NN»)  → пас §8
# Пас §7.5 написаний під німецьку модель (булети на book_pars + вставка
# «Пройди тест до уроку@» у FINAL). Для української він помилково булетив
# рефлексію («Оціни свої старання») і пхав тест у Фінал замість Додатків.
FINAL_IS_HOMEWORK = CURRENT_SUBJECT.startswith("Німецька мова")

ROOT              = pathlib.Path(__file__).parent
RAW_FILE          = ROOT / "lesson_raw.json"
GPT_OUT_DIR       = ROOT / "Text" / MODULE / "cells_gpt_out"
OUTPUT_FILE       = ROOT / "json_data" / "insert_actions.json"
BOLD_ANSWERS_FILE = ROOT / "json_data" / "test_bold_answers.json"

# --- Фільтр по конкретній клітинці (--cell) ---
CELL_FILTER_ROWS: set[str] | None = None
if args.cell:
    _all = sorted(GPT_OUT_DIR.glob("*.txt"))
    _spec = args.cell.strip()
    if ":" in _spec:
        _sec, _n_str = _spec.upper().split(":", 1)
        _n = int(_n_str)
        _sec_files = sorted(f for f in _all if f.stem.split("_")[-1].upper() == _sec)
        if 1 <= _n <= len(_sec_files):
            CELL_FILTER_ROWS = {_sec_files[_n - 1].stem.split("_")[1]}
        else:
            print(f"❌ --cell {args.cell}: секція {_sec} має лише {len(_sec_files)} файлів")
            sys.exit(1)
    else:
        CELL_FILTER_ROWS = {_spec}
    print(f"🔍 Фільтр клітинки: --cell {args.cell} → рядки {CELL_FILTER_ROWS}")

# ─── 1. Завантажуємо lesson_raw.json ─────────────────────────────────────────
cells_raw = json.loads(RAW_FILE.read_text(encoding="utf-8"))

# ─── 2. Індекси: (table, row) → параграфи COPYWRITER та INTEGRATOR ──────────
copywriter_index: dict[tuple, list] = {}
integrator_index: dict[tuple, list] = {}
for c in cells_raw:
    if c.get("kind") != "table_cell":
        continue
    key = (c["table"], c["row"])
    if c.get("column_name") == "### COPYWRITER":
        copywriter_index.setdefault(key, []).append(c)
    elif c.get("column_name") == "### INTEGRATOR":
        integrator_index.setdefault(key, []).append(c)
for idx in (copywriter_index, integrator_index):
    for key in idx:
        idx[key].sort(key=lambda c: c["start"])


# ─── 3. Маппінг: offset у joined-тексті → абсолютний індекс Google Doc ───────
def offset_to_doc_index(paragraphs: list, offset: int) -> tuple[int, dict | None]:
    """
    Повертає (doc_index, target_paragraph).
    doc_index — позиція для вставки у Google Doc.
    target_paragraph — параграф, у якому/після якого відбувається вставка
    (None якщо paragraphs порожній).

    Якщо offset == кінець параграфу або за межами тексту — target_paragraph
    є тим останнім параграфом (потрібен для перевірки list-стилю).
    """
    pos = 0
    for i, p in enumerate(paragraphs):
        seg_len = len(p["text"])
        if offset <= pos + seg_len:
            return p["start"] + (offset - pos), p
        pos += seg_len
        if i < len(paragraphs) - 1:
            pos += 1
    last = paragraphs[-1]
    return last["start"] + len(last["text"]), last


# ─── 4. Парсери маркерів ─────────────────────────────────────────────────────
INSERT_RE = re.compile(r'\[\[INSERT:(.*?)\]\]', re.DOTALL)
STYLE_RE   = re.compile(r'\*\*(.+?)\*\*|==(.+?)==', re.DOTALL)


def extract_styles(text: str) -> tuple[str, list[dict]]:
    """
    Знаходить **bold** та ==highlight== маркери у тексті.
    Повертає (clean_text, список {"start", "end", "kind"}) де позиції
    відносні до clean_text (маркери вже вилучені).
    """
    styles: list[dict] = []
    parts: list[str] = []
    clean_pos = 0
    last_end = 0

    for m in STYLE_RE.finditer(text):
        segment = text[last_end:m.start()]
        parts.append(segment)
        clean_pos += len(segment)

        if m.group(1) is not None:
            inner, kind = m.group(1), "bold"
        else:
            inner, kind = m.group(2), "highlight"

        styles.append({"start": clean_pos, "end": clean_pos + len(inner), "kind": kind})
        parts.append(inner)
        clean_pos += len(inner)
        last_end = m.end()

    parts.append(text[last_end:])
    return "".join(parts), styles


def extract_inserts(copywriter_text: str) -> list[tuple[int, str]]:
    """
    Повертає список (offset_in_original_text, insert_text) для кожного маркера.

    offset_in_original_text — позиція у тексті БЕЗ маркерів,
    тобто точка, куди треба вставити символ.
    insert_text — розкодований рядок (\\n → \n).

    Для однакових offset-ів маркери повертаються у ЗВОРОТНЬОМУ порядку,
    щоб change_Gdocs.py (sorted descending) застосував їх правильно:
    перший у документі вставляється останнім (не зсуває решту).
    """
    results = []
    clean_offset = 0
    last_end = 0

    for m in INSERT_RE.finditer(copywriter_text):
        # Текст між попереднім краєм (або початком) і цим маркером
        segment = copywriter_text[last_end:m.start()]
        clean_offset += len(segment.replace("⏎", ""))  # стрипаємо ⏎ — їх немає в Google Doc

        raw_value = m.group(1)
        # \\n → \n (ескейп з промпту); справжні \n залишаємо як є (re.DOTALL)
        insert_text = raw_value.replace("\\n", "\n").replace("\\t", "\t")
        results.append((clean_offset, insert_text))

        last_end = m.end()

    # Маркери на однаковій позиції → reverse, щоб apply-order був правильний
    # (change_Gdocs.py сортує за index desc → для рівних індексів перший у списку
    #  застосовується першим. Хочемо, щоб ОСТАННІЙ маркер застосовувався першим.)
    # Стабільне сортування: головне ключ = offset (збережемо), але
    # в межах однакового offset перевертаємо порядок появи.
    final = []
    i = 0
    while i < len(results):
        # знаходимо групу з однаковим offset
        j = i
        while j < len(results) and results[j][0] == results[i][0]:
            j += 1
        group = results[i:j]
        final.extend(reversed(group))  # розворот групи однакових offset-ів
        i = j

    return final


def _split_hw_by_conjunctions(sentence: str, page_re) -> list[str]:
    """
    Якщо речення ДЗ містить кілька посилань «с. NN», розбиває його на окремі
    пункти по сполучнику «та/і/й» між двома посиланнями.
    Повертає список частин (без «та/і/й» між ними, кожна з великої літери).
    """
    matches = list(page_re.finditer(sentence))
    if len(matches) < 2:
        return [sentence]
    # Шукаємо сполучник між першим і другим посиланням
    between_start = matches[0].end()
    between_end   = matches[1].start()
    between = sentence[between_start:between_end]
    m = re.search(r'\s+(та|і|й)\s+', between, re.IGNORECASE)
    if not m:
        return [sentence]
    part1 = sentence[:between_start + m.start()].strip()
    part2 = sentence[between_start + m.end():].strip()
    part2 = part2[0].upper() + part2[1:] if part2 else part2
    return [part1] + _split_hw_by_conjunctions(part2, page_re)


# ─── 5. Збір правильних відповідей з lesson_raw (для restyle-test) ───────────
# Зберігаємо ЗАРАЗ, поки doc ще не змінений і bold=True відображає оригінал.
test_bold_answers: dict[str, list[str]] = {}
if not args.restyle_test:
    for _c in cells_raw:
        if _c.get("kind") != "table_cell":
            continue
        if _c.get("column_name") != "### COPYWRITER":
            continue
        if (_c.get("section") or "").replace("###", "").strip() != "TEST":
            continue
        if not _c.get("bold"):
            continue
        _txt = _c.get("text", "").strip()
        if _txt:
            test_bold_answers.setdefault(f"{_c['table']}_{_c['row']}", []).append(_txt)

# ─── 6. Обхід cells_gpt_out ──────────────────────────────────────────────────
_CALLOUT_WORDS = {
    "занотуй", "запам'ятай", "важливо", "правило",
    "пригадай", "пригадаємо", "подумай", "увага", "спробуй", "зверни увагу",
}
actions = []
skipped = []


def _space_pair_dash(integrator_text: str) -> str:
    """Формат пар «термін-*відповідь*» → «термін - *відповідь*» (пробіли навколо тире).
    Тире перед `*` — роздільник пари; H5P парсить лише *...*, тире статичне. Ідемпотентно.
    Внутрішні дефіси слів (не перед `*`) не чіпаються."""
    return re.sub(r'(\S)[ \t]*-[ \t]*(\*)', r'\1 - \2', integrator_text)


def _number_h5p_body(integrator_text: str) -> str:
    """Нумеруємо рядки-завдання (1. 2. 3.) у блоках =FT= та =DT= після заголовка `h:`.
    Пропускаємо: `h:`, порожні рядки, вже пронумеровані, та рядки-перетягування слів
    (=DT= ВАРІАНТ В — починаються з '*'). Блоки =SCT=/=MCT=/=CW= не чіпаємо. Ідемпотентно.
    Детерміновано, бо Gemini цю нумерацію не робить стабільно."""
    if "=FT=" not in integrator_text and "=DT=" not in integrator_text:
        return integrator_text
    out, in_block, n = [], False, 0
    for ln in integrator_text.split("\n"):
        stripped = ln.strip()
        if stripped in ("=FT=", "=DT="):
            in_block, n = True, 0
            out.append(ln)
            continue
        if in_block:
            if re.fullmatch(r"=[A-ZА-ЯЇІЄҐ]{2,4}=", stripped):  # новий блок-маркер → кінець блоку
                in_block = False
                out.append(ln)
                continue
            if (not stripped or stripped.startswith("h:")
                    or re.match(r"^\d+\.\s", stripped) or stripped.startswith("*")):
                out.append(ln)  # заголовок / порожній / вже з номером / перетягування слів
                continue
            n += 1
            out.append(f"{n}. {ln}")
            continue
        out.append(ln)
    return "\n".join(out)


for txt_file in sorted(GPT_OUT_DIR.glob("*.txt")):
    if args.restyle_test:
        continue  # restyle mode — пропускаємо текстову заміну

    # --- Швидкий фільтр по секції через ім'я файлу ---
    name_section = txt_file.stem.split("_")[-1].upper()
    if SECTIONS_FILTER and name_section not in SECTIONS_FILTER:
        continue

    # --- Фільтр по конкретному рядку ---
    if CELL_FILTER_ROWS and txt_file.stem.split("_")[1] not in CELL_FILTER_ROWS:
        continue

    content = txt_file.read_text(encoding="utf-8")

    # --- CELL_ID → (table, row, section) ---
    id_match = re.search(r'^CELL_ID:\s*(.+)$', content, re.MULTILINE)
    cell_id = id_match.group(1).strip() if id_match else txt_file.stem

    parts = cell_id.split("_", 2)
    if len(parts) < 2:
        skipped.append(f"{txt_file.name}: некоректний CELL_ID '{cell_id}'")
        continue
    try:
        table, row = int(parts[0]), int(parts[1])
    except ValueError:
        skipped.append(f"{txt_file.name}: не вдалося розпарсити table/row з '{cell_id}'")
        continue
    section = parts[2] if len(parts) > 2 else ""

    key = (table, row)
    is_test = section == "TEST"

    # --- Витягуємо секції COPYWRITER та INTEGRATOR ---
    _THEORIA_FAMILY = {"THEORIA", "FACT", "EXAMPLES"}
    copy_match = re.search(r'### COPYWRITER\n(.*?)(?=\n### |\Z)', content, re.DOTALL)
    # Gemini іноді пише назву секції замість ### COPYWRITER (характерно для теорії)
    if not copy_match and section in _THEORIA_FAMILY:
        copy_match = re.search(rf'### {section}\n(.*?)(?=\n### |\Z)', content, re.DOTALL)
    int_match  = re.search(r'### INTEGRATOR\n(.*?)(?=\n### |\Z)', content, re.DOTALL)

    if copy_match:
        copywriter_text = copy_match.group(1).rstrip()
    elif is_test:
        # TEST-файл без заголовка — весь вміст (без рядків ### ...) є COPYWRITER
        copywriter_text = re.sub(r'^###[^\n]*\n?', '', content, flags=re.MULTILINE).strip()
    else:
        copywriter_text = ""
    integrator_text = int_match.group(1).rstrip() if int_match else ""
    # Детерміновані формальні трансформи (Gemini їх не робить стабільно):
    integrator_text = _space_pair_dash(integrator_text)        # пари: «термін - *відповідь*»
    integrator_text = _number_h5p_body(integrator_text)        # =FT=/=DT=: нумерація 1. 2. 3.

    has_copy_inserts = "[[INSERT:" in copywriter_text
    has_int_content  = bool(integrator_text)

    if not has_copy_inserts and not has_int_content and not is_test:
        continue  # клітинка не оброблена або порожня

    # --- COPYWRITER: маркерні вставки ---
    if has_copy_inserts:
        paragraphs = copywriter_index.get(key)
        if not paragraphs:
            skipped.append(f"{txt_file.name}: COPYWRITER key {key} не знайдений у lesson_raw.json")
        else:
            # [bold] — мітка для GPT, не є частиною тексту документа.
            # Видаляємо будь-який сміття що GPT міг поставити перед [bold]:
            # [[INSERT:??]], [[INSERT:!!]], ** тощо.
            _cw = re.sub(r'^(?:\[\[INSERT:[^\]]*\]\]|\*{1,2} ?)?\[bold\] ', '', copywriter_text, flags=re.MULTILINE)
            # [[INSERT:!!]] в THEORIA — секція 7 додає !! детерміновано, маркер ігноруємо
            if section in _THEORIA_FAMILY:
                _cw = _cw.replace("[[INSERT:!!]]", "")
            inserts = extract_inserts(_cw)
            for offset, insert_text in inserts:
                # ── Препроцесинг «Перевір себе» до extract_styles ───
                _PS = "Перевір себе"
                _ps_has_bullets = False
                if _PS in insert_text:
                    # Два \n перед заголовком для відступу
                    insert_text = "\n\n" + insert_text.lstrip("\n")
                    # Стрипаємо • з рядків → замінимо на create_bullet
                    _it_lines = insert_text.split("\n")
                    if any(l.startswith("•") for l in _it_lines):
                        _ps_has_bullets = True
                        insert_text = "\n".join(
                            l[2:] if l.startswith("• ") else (l[1:] if l.startswith("•") else l)
                            for l in _it_lines
                        )
                # ────────────────────────────────────────────────────
                doc_idx, target_par = offset_to_doc_index(paragraphs, offset)
                clean_insert, insert_styles = extract_styles(insert_text)
                post_styles = [
                    {"relative_start": s["start"], "relative_end": s["end"], "kind": s["kind"]}
                    for s in insert_styles
                ]
                # delete_bullet ПЕРЕД create_bullet — прибирає унаслідуваний стиль списку
                if target_par and target_par.get("list") and "\n" in clean_insert:
                    first_nl = clean_insert.index("\n")
                    post_styles.append({
                        "relative_start": first_nl + 1,
                        "relative_end": len(clean_insert),
                        "kind": "delete_bullet",
                    })
                # «Перевір себе»: жирний заголовок + create_bullet для • рядків
                if _PS in clean_insert:
                    _i = clean_insert.index(_PS)
                    if not any(s["relative_start"] == _i and s["kind"] == "bold" for s in post_styles):
                        post_styles.append({"relative_start": _i, "relative_end": _i + len(_PS), "kind": "bold"})
                    if _ps_has_bullets:
                        _ps_end = _i + len(_PS)
                        _nl_idx = clean_insert[_ps_end:].find("\n")
                        if _nl_idx != -1:
                            _first_item = _ps_end + _nl_idx + 1
                            if _first_item < len(clean_insert):
                                post_styles.append({
                                    "relative_start": _first_item,
                                    "relative_end": len(clean_insert),
                                    "kind": "create_bullet",
                                })
                if section in _THEORIA_FAMILY and target_par is not None:
                    char_offset = doc_idx - target_par["start"]
                    actions.append({
                        "action": "insert_in_paragraph",
                        "paragraph_text": target_par["text"],
                        "char_offset": char_offset,
                        # очікуваний абс. індекс COPYWRITER-абзацу (з lesson_raw) —
                        # щоб change_Gdocs при ДУБЛІ тексту (вчительська колонка теж
                        # містить цей заголовок) обрав найближче входження, а не перше.
                        "expected_index": target_par["start"],
                        "text": clean_insert,
                        "post_styles": post_styles,
                        "_cell_id": cell_id,
                    })
                    print(f"  [{cell_id}] CW THEORIA para={repr(target_par['text'][:40])} offset={char_offset} | insert={repr(clean_insert)}")
                else:
                    actions.append({
                        "action": "insert_text",
                        "index": doc_idx,
                        "text": clean_insert,
                        "post_styles": post_styles,
                        "_cell_id": cell_id,
                    })
                    print(f"  [{cell_id}] CW offset={offset} -> doc_idx={doc_idx} | insert={repr(clean_insert)}")

    # --- INTEGRATOR: маркери або повний текст ---
    if has_int_content:
        paragraphs_int = integrator_index.get(key)
        if not paragraphs_int:
            skipped.append(f"{txt_file.name}: INTEGRATOR key {key} не знайдений у lesson_raw.json")
        elif "[[INSERT:" in integrator_text:
            # Маркерний підхід (наприклад [[INSERT:=АА=]] у APPENDIX)
            int_inserts = extract_inserts(integrator_text)
            for offset, insert_text in int_inserts:
                doc_idx, _ = offset_to_doc_index(paragraphs_int, offset)
                actions.append({
                    "action": "insert_text",
                    "index": doc_idx,
                    "text": insert_text,
                    "_cell_id": cell_id,
                })
                print(f"  [{cell_id}] INT offset={offset} -> doc_idx={doc_idx} | insert={repr(insert_text)}")
        else:
            # Точкова / повна заміна INTEGRATOR
            clean_int = integrator_text.replace("⏎", "")
            sorted_pars = sorted(paragraphs_int, key=lambda p: p["start"])
            cell_start = sorted_pars[0]["start"]
            cell_end   = sorted_pars[-1]["end"] - 1  # зберігаємо фінальний \n клітинки

            # Існуючий текст клітинки (без trailing \n кожного параграфа)
            existing_text = "\n".join(p["text"] for p in sorted_pars if p.get("text"))

            # Якщо новий текст є розширенням існуючого — лише INSERT суфікса.
            # Використовуємо append_to_cell (без фіксованого індексу) — change_Gdocs.py
            # перечитає свіжий GDoc після всіх інших операцій щоб уникнути зсуву позицій.
            if clean_int.startswith(existing_text) and len(clean_int) > len(existing_text):
                suffix = clean_int[len(existing_text):]
                actions.append({
                    "action": "append_to_cell",
                    "table": sorted_pars[0]["table"],
                    "row":   sorted_pars[0]["row"],
                    "col":   sorted_pars[0]["col"],
                    "text":  suffix,
                    "_cell_id": cell_id,
                })
                print(f"  [{cell_id}] INT append (live pos) | suffix={repr(suffix[:60])}")
            else:
                # Повна заміна: видаляємо старе, вставляємо нове
                if cell_end > cell_start:
                    actions.append({
                        "action": "delete_range",
                        "index": cell_start,
                        "end_index": cell_end,
                        "_cell_id": cell_id,
                    })
                actions.append({
                    "action": "insert_text",
                    "index": cell_start,
                    "text": clean_int,
                    "_cell_id": cell_id,
                })
                print(f"  [{cell_id}] INT replace at doc_idx={cell_start}..{cell_end} | text={repr(clean_int[:60])}")

    # --- TEST: повна заміна COPYWRITER ---
    if is_test and copywriter_text and not has_copy_inserts:
        paragraphs = copywriter_index.get(key)
        if not paragraphs:
            skipped.append(f"{txt_file.name}: COPYWRITER key {key} не знайдений у lesson_raw.json")
        else:
            clean_copy = copywriter_text.replace("⏎", "")
            clean_copy, style_infos = extract_styles(clean_copy)  # ручні маркери **...** ==...==
            sorted_pars = sorted(paragraphs, key=lambda p: p["start"])
            cell_start = sorted_pars[0]["start"]
            cell_end   = sorted_pars[-1]["end"] - 1

            # Збираємо post_styles з відносними позиціями від cell_start
            post_styles: list[dict] = []

            for s in style_infos:
                post_styles.append({"relative_start": s["start"], "relative_end": s["end"], "kind": s["kind"]})
                # ==...== у TEST → правильна відповідь: лише highlight (без bold)
                # bold додасться нижче тільки якщо рядок є серед bold_texts

            # Авто: bold-параграфи з lesson_raw → зелений фон для правильної відповіді.
            # ВАЖЛИВО: матчимо ПО-ПИТАННЮ, а не за глобальним текстом — інакше варіант, що є
            # правильним в одному питанні, підсвічувався б і в інших, де його текст повторюється
            # (напр. "der Bus"/"das Velo" у різних питаннях → хибні зайві правильні).
            _QNUM_RE = re.compile(r'^\s*(\d+)\.')
            _ANSWER_RE = re.compile(r'^[А-ЯІЇЄҐа-яіїєґA-Za-z]\)\s')
            # bold-відповіді, згруповані за номером питання (з lesson_raw)
            bold_by_q: dict[str, set] = {}
            _cur_q = None
            for p in sorted_pars:
                _pt = (p.get("text") or "").strip()
                if not _pt:
                    continue
                _qm = _QNUM_RE.match(_pt)
                if _qm:
                    _cur_q = _qm.group(1)
                elif p.get("bold") and _cur_q is not None:
                    bold_by_q.setdefault(_cur_q, set()).add(_pt)

            # вже підсвічені діапазони (дедуп з ==-маркерами Gemini)
            _hl_spans = {(s["relative_start"], s["relative_end"])
                         for s in post_styles if s["kind"] == "highlight"}
            if bold_by_q:
                pos = 0
                _cur_q = None
                for line in clean_copy.split("\n"):
                    line_stripped = line.strip()
                    _qm = _QNUM_RE.match(line_stripped) if line_stripped else None
                    if _qm:
                        _cur_q = _qm.group(1)
                    else:
                        _am = _ANSWER_RE.match(line_stripped) if line_stripped else None
                        if _am and _cur_q is not None:
                            answer_content = line_stripped[_am.end():]
                            if answer_content in bold_by_q.get(_cur_q, set()):
                                span = (pos, pos + len(line))
                                if span not in _hl_spans:
                                    post_styles.append({"relative_start": pos, "relative_end": pos + len(line), "kind": "highlight"})
                                    _hl_spans.add(span)
                    pos += len(line) + 1

            # Google Docs наслідує bold від точки вставки →
            # явно знімаємо bold з усього тексту, потім повертаємо першому рядку
            leading: list[dict] = [
                {"relative_start": 0, "relative_end": len(clean_copy), "kind": "unbold"},
            ]
            if sorted_pars[0].get("bold") and clean_copy:
                first_nl = clean_copy.find("\n")
                leading.append({
                    "relative_start": 0,
                    "relative_end": first_nl if first_nl != -1 else len(clean_copy),
                    "kind": "bold",
                })
            post_styles = leading + post_styles

            if cell_end > cell_start:
                actions.append({
                    "action": "delete_range",
                    "index": cell_start,
                    "end_index": cell_end,
                    "_cell_id": cell_id,
                })
            actions.append({
                "action": "insert_text",
                "index": cell_start,
                "text": clean_copy,
                "post_styles": post_styles,
                "_cell_id": cell_id,
            })
            print(f"  [{cell_id}] CW TEST replace at doc_idx={cell_start}..{cell_end} | text={repr(clean_copy[:60])}")

# ─── 6. Видалення числових префіксів «N. » у PRACTICA COPYWRITER ─────────────
# Виконується тільки якщо --sections містить PRACTICA (або --sections не задано)
if not args.restyle_test and (SECTIONS_FILTER is None or "PRACTICA" in SECTIONS_FILTER):
    NUM_PREFIX_RE = re.compile(r'^\d+\.\s*')
    for c in cells_raw:
        if c.get("kind") != "table_cell" or c.get("column_name") != "### COPYWRITER":
            continue
        section = (c.get("section") or "").replace("###", "").strip()
        if section != "PRACTICA":
            continue
        if CELL_FILTER_ROWS and str(c["row"]) not in CELL_FILTER_ROWS:
            continue
        m = NUM_PREFIX_RE.match(c.get("text", ""))
        if not m:
            continue
        prefix_len = len(m.group(0))
        actions.append({
            "action": "delete_range",
            "index": c["start"],
            "end_index": c["start"] + prefix_len,
            "_cell_id": f"{c['table']}_{c['row']}_PRACTICA",
        })
        print(f"  [{c['table']}_{c['row']}] CW del '{m.group(0)}' at doc_idx={c['start']}")

# ─── 7. THEORIA/FACT/EXAMPLES: маркери для bold-параграфів ────────────────────
# Детерміновано з lesson_raw (bold=True), без участі GPT:
#   Занотуй / Запам'ятай / Важливо / Правило  →  вставити ! або !! → !!
#   перший bold не-заклик, не питання           →  вставити ** → **
#   перший bold не-заклик, питання              →  ?? від GPT (секція 7 не чіпає)
_THEORIA_SECTS = {"THEORIA", "FACT", "EXAMPLES"}
# DICTIONARY теж сканується на callout-слова (Запам'ятай! → !!), але БЕЗ **-заголовків.
_CALLOUT_SECTS = _THEORIA_SECTS | {"DICTIONARY"}
if not args.restyle_test and (SECTIONS_FILTER is None or _CALLOUT_SECTS & SECTIONS_FILTER):
    for (_t, _r), _pars in copywriter_index.items():
        if not _pars:
            continue
        _sec = (_pars[0].get("section") or "").replace("###", "").strip().upper()
        if _sec not in _CALLOUT_SECTS:
            continue
        if SECTIONS_FILTER and _sec not in SECTIONS_FILTER:
            continue
        if CELL_FILTER_ROWS and str(_r) not in CELL_FILTER_ROWS:
            continue

        _cid = f"{_t}_{_r}_{_sec}"

        for _i, _p in enumerate(_pars):
            if not _p.get("text", "").strip():
                continue
            _body = _p["text"].rstrip("\n")
            _insert_at = _p["end"] - 1  # перед фінальним \n
            if _insert_at <= _p["start"]:
                continue

            # ── Заклики: Занотуй / Запам'ятай / Пригадай / Подумай тощо → !!
            # Нормалізуємо апостроф: у doc буває типографський ’ (U+2019)/ʼ (U+02BC),
            # а _CALLOUT_WORDS — зі звичайним ' (U+0027), інакше «запам’ятай» не матчиться.
            _normalized = _body.lower().rstrip("!?").strip().replace("’", "'").replace("ʼ", "'")
            if _normalized in _CALLOUT_WORDS:
                if _body.endswith("!!"):
                    continue  # вже є
                # Пригадаємо? → ?!! ; Занотуй! → !! ; Подумай → !!
                _extra = "!" if _body.endswith("!") else "!!"
                # Якщо заклик-параграф жирний — доданий «!» теж робимо жирним,
                # щоб «!!» виглядав однорідно (інакше change_Gdocs скидає його стиль).
                _callout_styles = (
                    [{"relative_start": 0, "relative_end": len(_extra), "kind": "bold"}]
                    if _p.get("bold") else []
                )
                actions.append({
                    "action": "insert_text",
                    "index": _insert_at,
                    "text": _extra,
                    "post_styles": _callout_styles,
                    "_cell_id": _cid,
                })
                print(f"  [{_cid}] insert '{_extra}' (callout!!) | '{_body}'")
                continue

            # ── Bold-заголовок (**): лише для THEORIA/FACT/EXAMPLES, не для DICTIONARY
            if _sec not in _THEORIA_SECTS:
                continue
            # ── Bold-заголовок: тільки самий перший параграф клітинки
            if _i != 0:
                continue  # ** тільки для першого параграфа
            if not _p.get("bold"):
                continue
            if _body.endswith("?") or _body.endswith("??"):
                continue  # питання → ?? від GPT
            if _body.endswith("**"):
                continue  # вже є
            actions.append({
                "action": "insert_text",
                "index": _insert_at,
                "text": "**",
                "post_styles": [],
                "_cell_id": _cid,
            })
            print(f"  [{_cid}] insert '**' (heading) | '{_body[:40]}'")

# ─── 8. Реформатування «Домашнє завдання» у APPENDIX ────────────────────────
# Детерміновано (без Gemini): розбиває речення на окремі рядки + #PAGE маркер.
if not args.restyle_test and (SECTIONS_FILTER is None or "APPENDIX" in SECTIONS_FILTER):
    _HW_HEADER_RE  = re.compile(r'Домашнє завдання\s*:')
    _PAGE_RE       = re.compile(r'(?<!\[)с\.\s*(\d+(?:[-–]\d+)?)')  # не матчить всередині [с. NN]
    _SENTENCE_SPLIT = re.compile(r'\.\s+(?=[А-ЯІЇЄҐ])')
    # ДЗ без заголовка: вчитель часто пише одразу завдання («Опрацюй посібник
    # частина 5, с. 42–44»), без рядка «Домашнє завдання:». Тоді заголовок
    # додаємо самі — інакше пас не спрацьовував і ДЗ лишалось сирим текстом.
    _HW_VERB_RE    = re.compile(r'(опрацю|виконай|виконати|прочита|повтори)', re.IGNORECASE)

    for c in cells_raw:
        if c.get("kind") != "table_cell" or c.get("column_name") != "### COPYWRITER":
            continue
        if (c.get("section") or "").replace("###", "").strip() != "APPENDIX":
            continue
        text = c.get("text", "")
        hm = _HW_HEADER_RE.search(text)
        if hm:
            header_prefix = ""
            colon_pos     = hm.end()                  # позиція одразу після ":"
            hw_raw        = text[colon_pos:].strip().rstrip(".")
        else:
            # Заголовка нема — ДЗ пізнаємо за дієсловом + посиланням на сторінку,
            # і додаємо «Домашнє завдання:» самі. Тіло — уся клітинка.
            if not (_HW_VERB_RE.search(text) and _PAGE_RE.search(text)):
                continue
            header_prefix = "Домашнє завдання:"
            colon_pos     = 0
            hw_raw        = text.strip().rstrip(".")
        if not hw_raw:
            continue

        sentences = _SENTENCE_SPLIT.split(hw_raw)
        sentences = [s.strip().rstrip(".") for s in sentences if s.strip()]
        sentences = [s[0].upper() + s[1:] if s else s for s in sentences]

        formatted = []
        for s in sentences:
            for part in _split_hw_by_conjunctions(s, _PAGE_RE):
                pm = _PAGE_RE.search(part)
                if pm:
                    first_page = pm.group(1).split("-")[0].split("–")[0]
                    part_linked = part[:pm.start()] + f"[{pm.group(0)}]" + part[pm.end():]
                    formatted.append(f"{part_linked} #{first_page}")
                else:
                    formatted.append(part)

        new_text  = header_prefix + "\n" + "\n".join(formatted)
        doc_after_colon = c["start"] + colon_pos
        doc_end         = c["end"] - 1               # зберігаємо \n параграфу

        hw_post_styles = [{
            "relative_start": len(header_prefix) + 1,   # після першого \n
            "relative_end":   len(new_text),
            "kind":           "create_bullet",
        }]

        cell_id = f"{c['table']}_{c['row']}_APPENDIX"
        if doc_end > doc_after_colon:
            actions.append({
                "action":    "delete_range",
                "index":     doc_after_colon,
                "end_index": doc_end,
                "_cell_id":  cell_id,
            })
        actions.append({
            "action": "insert_text",
            "index":  doc_after_colon,
            "text":   new_text,
            "post_styles": hw_post_styles,
            "_cell_id": cell_id,
        })
        print(f"  [{cell_id}] HW → {len(sentences)} рядків: {formatted}")

        cw_col = c["col"]
        appendix_cells = [
            c2 for c2 in cells_raw
            if c2.get("kind") == "table_cell"
            and c2.get("table") == c["table"]
            and c2.get("col") == cw_col
            and (c2.get("section") or "").replace("###", "").strip() == "APPENDIX"
        ]

        if any("Пройди тест до уроку" in c2.get("text", "") for c2 in appendix_cells):
            print(f"  [{cell_id}] APPENDIX → 'Пройди тест до уроку@' already exists, skip")
            continue

        appendix_rows = sorted({int(c2["row"]) for c2 in appendix_cells})
        target_row = appendix_rows[-1] if appendix_rows else c["row"]

        target_row_pars = sorted(
            [c2 for c2 in cells_raw
             if c2.get("kind") == "table_cell"
             and c2.get("table") == c["table"]
             and c2.get("row") == target_row
             and c2.get("col") == cw_col],
            key=lambda p: p["start"],
        )
        target_is_empty = target_row_pars and all(
            p.get("text", "").strip() == "" for p in target_row_pars
        )
        if target_is_empty:
            first_par = target_row_pars[0]
            actions.append({
                "action":   "insert_text",
                "index":    first_par["start"],
                "text":     "Пройди тест до уроку@",
                "_cell_id": f"{c['table']}_{target_row}_APPENDIX",
            })
            print(f"  [{c['table']}_{target_row}] APPENDIX → 'Пройди тест до уроку@' (end of APPENDIX, empty row)")
        else:
            actions.append({
                "action":    "insert_table_row",
                "index":     999_999_999,   # обробляється першим (вищий за всі doc-індекси)
                "table_idx": c["table"],
                "after_row": target_row,
                "cw_col":    cw_col,
                "text":      "Пройди тест до уроку@",
                "_cell_id":  f"{c['table']}_{target_row + 1}_APPENDIX",
            })
            print(f"  [{c['table']}_{target_row}] APPENDIX → new row after APPENDIX with 'Пройди тест до уроку@'")

# ─── 8.1 APPENDIX: відео-підпис «...відео може не відтворюватись» → додати @ в кінець ──
# Детерміновано: doc_to_html реагує на @ (обгортає у task_atention-блок). Тут лише ставимо @.
if not args.restyle_test and (SECTIONS_FILTER is None or "APPENDIX" in SECTIONS_FILTER):
    for c in cells_raw:
        if c.get("kind") != "table_cell" or c.get("column_name") != "### COPYWRITER":
            continue
        if (c.get("section") or "").replace("###", "").strip() != "APPENDIX":
            continue
        if CELL_FILTER_ROWS and str(c["row"]) not in CELL_FILTER_ROWS:
            continue
        _body = (c.get("text") or "").rstrip("\n")
        if "відео може не відтворюватись" not in _body.lower():
            continue
        if "@" in _body:
            continue  # вже є — ідемпотентно
        _insert_at = c["end"] - 1  # перед фінальним \n параграфа
        if _insert_at <= c["start"]:
            continue
        actions.append({
            "action": "insert_text",
            "index": _insert_at,
            "text": "@",
            "post_styles": [],
            "_cell_id": f"{c['table']}_{c['row']}_APPENDIX",
        })
        print(f"  [{c['table']}_{c['row']}_APPENDIX] insert '@' (video caption) | {repr(_body[:45])}")

# ─── 8.2 INTERACTIVE: standalone <iframe> ПЕРЕД назвою → своп (назва, потім iframe) ──
# Реордер у самому документі: delete+insert (як INTEGRATOR-replace). Ідемпотентно:
# спрацьовує лише коли iframe стоїть перед текстом-назвою.
_IFRAME_RE = re.compile(r'^\s*<iframe[^>]*>.*</iframe>\s*$', re.IGNORECASE | re.DOTALL)
if not args.restyle_test and (SECTIONS_FILTER is None or "INTERACTIVE" in SECTIONS_FILTER):
    for (_t, _r), _pars in copywriter_index.items():
        if not _pars:
            continue
        if (_pars[0].get("section") or "").replace("###", "").strip().upper() != "INTERACTIVE":
            continue
        if CELL_FILTER_ROWS and str(_r) not in CELL_FILTER_ROWS:
            continue
        _sorted = sorted(_pars, key=lambda p: p["start"])
        for _i in range(len(_sorted) - 1):
            _ifr, _ttl = _sorted[_i], _sorted[_i + 1]
            _ifr_text = (_ifr.get("text") or "").strip()
            _ttl_text = (_ttl.get("text") or "").strip()
            if not _IFRAME_RE.match(_ifr_text):
                continue
            # назва: непорожня, містить літери, сама не iframe
            if not _ttl_text or _IFRAME_RE.match(_ttl_text):
                continue
            if not re.search(r'[А-ЯҐЄІЇа-яґєіїA-Za-z]', _ttl_text):
                continue
            # абзаци мають бути сусідні в документі
            if _ttl["start"] != _ifr["end"]:
                continue
            _start = _ifr["start"]
            _del_end = _ttl["end"] - 1   # лишаємо один термінальний \n
            if _del_end <= _start:
                continue
            _cid = f"{_t}_{_r}_INTERACTIVE"
            actions.append({
                "action": "delete_range",
                "index": _start,
                "end_index": _del_end,
                "_cell_id": _cid,
            })
            actions.append({
                "action": "insert_text",
                "index": _start,
                "text": f"{_ttl_text}\n{_ifr_text}",
                "post_styles": [],
                "_cell_id": _cid,
            })
            print(f"  [{_cid}] swap iframe↔назва | назва={repr(_ttl_text[:30])}")
            break  # один своп на рядок

# ─── 7.5 FINAL: видалити Drive-посилання, маркований список + тест ──────────
# Лише для предметів, де ДЗ лежить у FINAL (німецька). В українській ДЗ у
# APPENDIX — там його оформлює §8, він же додає «Пройди тест до уроку@».
if not args.restyle_test and FINAL_IS_HOMEWORK and (SECTIONS_FILTER is None or "FINAL" in SECTIONS_FILTER):
    _DRIVE_RE = re.compile(r'https?://(drive|docs)\.google\.com')
    # s.23 / S. 23 / s.23-25 → [s.23] (не чіпає вже відформатовані [s.NN])
    _S_PAGE_RE = re.compile(r'(?<!\[)[Ss]\.?\s*(\d+(?:[–\-]\d+)?)')

    final_groups: dict[tuple, list] = {}
    for _c in cells_raw:
        if _c.get("kind") != "table_cell":
            continue
        if _c.get("column_name") != "### COPYWRITER":
            continue
        if (_c.get("section") or "").replace("###", "").strip() != "FINAL":
            continue
        _key = (_c["table"], _c["row"])
        final_groups.setdefault(_key, []).append(_c)

    # Дедуп «Пройди тест до уроку» ПО ВСІЙ FINAL-секції (а не по окремому рядку),
    # інакше рядок без нього додає дубль, коли інший FINAL-рядок його вже має.
    _final_has_test = any(
        "Пройди тест до уроку" in p.get("text", "")
        for _pars in final_groups.values() for p in _pars
    )

    for (tbl, rw), final_pars in final_groups.items():
        final_pars = sorted(final_pars, key=lambda p: p["start"])
        cell_id = f"{tbl}_{rw}_FINAL"

        if any("Пройди тест до уроку" in p.get("text", "") for p in final_pars):
            print(f"  [{cell_id}] вже є 'Пройди тест до уроку', пропускаємо")
            continue

        drive_pars = [p for p in final_pars if _DRIVE_RE.search(p.get("text", ""))]
        book_pars  = [p for p in final_pars
                      if p.get("text", "").strip()
                      and not _DRIVE_RE.search(p.get("text", ""))
                      and "Hausaufgaben" not in p.get("text", "")]

        # Видалити Drive-рядки
        for dp in drive_pars:
            if dp["end"] > dp["start"]:
                actions.append({
                    "action":    "delete_range",
                    "index":     dp["start"],
                    "end_index": dp["end"],
                    "_cell_id":  cell_id,
                })
                print(f"  [{cell_id}] FINAL → delete Drive [{dp['start']}, {dp['end']})")

        # Форматуємо book_pars: s.NN → [s.NN], застосовуємо маркований список
        if book_pars:
            for bp in book_pars:
                orig_text = bp.get("text", "").rstrip("\n")
                reformatted = _S_PAGE_RE.sub(lambda m: f"[s.{m.group(1)}]", orig_text)
                _pm = _S_PAGE_RE.search(orig_text)
                if _pm:
                    first_page = _pm.group(1).split("-")[0].split("–")[0]
                    reformatted += f" #{first_page}"
                bp_end = bp["end"] - 1  # перед фінальним \n
                if bp_end > bp["start"]:
                    actions.append({
                        "action":    "delete_range",
                        "index":     bp["start"],
                        "end_index": bp_end,
                        "_cell_id":  cell_id,
                    })
                actions.append({
                    "action":   "insert_text",
                    "index":    bp["start"],
                    "text":     reformatted,
                    "post_styles": [{
                        "relative_start": 0,
                        "relative_end":   len(reformatted),
                        "kind":           "create_bullet",
                    }],
                    "_cell_id": cell_id,
                })
                print(f"  [{cell_id}] FINAL book → '{reformatted}' + bullet at {bp['start']}")

        # «Пройди тест до уроку@» — новий рядок таблиці після поточного FINAL рядка.
        # Додаємо навіть без book_pars (напр. ДЗ задано лише Drive-файлом, який видалили):
        # урок усе одно лишається з посиланням на тест.
        if not _final_has_test:
            cw_col = (book_pars[0] if book_pars else final_pars[0]).get("col", 0)
            final_rows_all = sorted({int(p["row"]) for p in final_pars})
            target_row = final_rows_all[-1]
            actions.append({
                "action":    "insert_table_row",
                "index":     999_999_999,
                "table_idx": tbl,
                "after_row": target_row,
                "cw_col":    cw_col,
                "text":      "Пройди тест до уроку@",
                "_cell_id":  f"{tbl}_{target_row + 1}_FINAL",
            })
            _final_has_test = True  # додаємо щонайбільше один раз на секцію
            print(f"  [{cell_id}] FINAL → new row after {target_row} with 'Пройди тест до уроку@'")

# ─── 8. Збереження bold-відповідей після нормального run ─────────────────────
if not args.restyle_test and test_bold_answers:
    BOLD_ANSWERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    BOLD_ANSWERS_FILE.write_text(
        json.dumps(test_bold_answers, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  Saved bold answers → {BOLD_ANSWERS_FILE.name} ({len(test_bold_answers)} клітинок)")

# ─── 8. Restyle TEST: bold+highlight для правильних відповідей ───────────────
if args.restyle_test:
    if not BOLD_ANSWERS_FILE.exists():
        print(f"⚠️  {BOLD_ANSWERS_FILE.name} не знайдений.")
        print("   Спочатку запусти нормальний run: python cells_to_actions.py")
    else:
        bold_answers = json.loads(BOLD_ANSWERS_FILE.read_text(encoding="utf-8"))
        for c in cells_raw:
            if c.get("kind") != "table_cell":
                continue
            if c.get("column_name") != "### COPYWRITER":
                continue
            if (c.get("section") or "").replace("###", "").strip() != "TEST":
                continue
            txt = c.get("text", "").strip()
            if not txt:
                continue
            key = f"{c['table']}_{c['row']}"
            bold_texts_for_cell = bold_answers.get(key, [])
            # Точний збіг: bt зберігається як повний рядок відповіді ("A) auf"),
            # тому порівнюємо txt == bt, щоб не підсвічувати однакові слова в інших варіантах.
            if not any(bt and bt == txt for bt in bold_texts_for_cell):
                continue
            start = c["start"]
            end = c["end"] - 1
            if end <= start:
                continue
            for kind in ("bold", "highlight"):
                actions.append({
                    "action": "format_text",
                    "index": start,
                    "end_index": end,
                    "kind": kind,
                    "_cell_id": f"{key}_TEST",
                })
            print(f"  [{key}] RESTYLE bold+highlight at {start}..{end} | {repr(txt[:50])}")

# ─── 9. Верифікація колонок ──────────────────────────────────────────────────
_SAFE_COLS = {"### COPYWRITER", "### INTEGRATOR"}
_SKIP_ACTIONS = {"insert_table_row", "insert_in_paragraph", "format_text", "append_to_cell"}

def _col_for_idx(idx: int):
    """Повертає (column_name, table, row) параграфа що містить idx, або (None,None,None)."""
    for _c in cells_raw:
        if _c.get("kind") != "table_cell":
            continue
        if _c["start"] <= idx < _c["end"]:
            return _c.get("column_name"), _c.get("table"), _c.get("row")
    return None, None, None

_verify_ok = True
for _act in actions:
    if _act["action"] in _SKIP_ACTIONS:
        continue
    _checks: list[tuple[str, int]] = []
    if "index" in _act and _act["index"] < 999_999_999:
        _checks.append(("index", _act["index"]))
    if "end_index" in _act:
        _checks.append(("end_index", _act["end_index"] - 1))
    for _key, _idx in _checks:
        _col, _t, _r = _col_for_idx(_idx)
        if _col is None:
            print(f"  ⚠️  [{_act.get('_cell_id','?')}] {_key}={_idx} — не знайдено в жодній клітинці")
            _verify_ok = False
        elif _col not in _SAFE_COLS:
            print(f"  ❌  [{_act.get('_cell_id','?')}] {_key}={_idx} → {_col} (t={_t}, r={_r}) — ЗАБОРОНЕНА КОЛОНКА!")
            _verify_ok = False

if _verify_ok:
    print("✅ Верифікація колонок: OK")
else:
    print("\n❌ ВЕРИФІКАЦІЯ ПРОВАЛЕНА — insert_actions.json НЕ збережено")
    print("   Перезапусти generate_symbol.py і спробуй знову")
    sys.exit(1)

# ─── 9.5. Наскрізна нумерація заглушок H5P ────────────────────────────────────
# Gemini нумерує заглушку в межах клітинки (кожна практика дає «заглушка1»), через що
# номери плейсхолдерів колізують. Перенумеровуємо детерміновано 1, 2, 3... у порядку
# дій (= порядок клітинок), щоб збігалося з порядком H5P-завдань у doc_to_html.
_zag = [0]
def _renumber_zaglushka(_m):
    _zag[0] += 1
    return f"заглушка{_zag[0]}"
for _a in actions:
    _txt = _a.get("text")
    if _txt and "заглушка" in _txt:
        _a["text"] = re.sub(r"заглушка\d+", _renumber_zaglushka, _txt)

# ─── 10. Запис результату ─────────────────────────────────────────────────────
print(f"\n{'-'*60}")
print(f"Всього дій: {len(actions)}")

if skipped:
    print(f"\n⚠️  Пропущено {len(skipped)} файлів:")
    for s in skipped:
        print(f"   • {s}")

if args.dry_run:
    print("\n[dry-run] Файл НЕ збережено.")
else:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(actions, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\nOK  Saved: {OUTPUT_FILE}")
    print("   Run next: python change_Gdocs.py")

# ─── 11. Snapshot для відкату ─────────────────────────────────────────────────
ROLLBACK_FILE = ROOT / "json_data" / "rollback_snapshot.json"

if not args.dry_run and not args.restyle_test:
    touched_keys: set[tuple] = set()
    inserted_rows_snap: list[dict] = []
    for act in actions:
        cid = act.get("_cell_id", "")
        parts = cid.split("_", 2)
        if len(parts) >= 2:
            try:
                touched_keys.add((int(parts[0]), int(parts[1])))
            except ValueError:
                pass
        if act["action"] == "insert_table_row":
            inserted_rows_snap.append({
                "table_idx": act["table_idx"],
                "after_row":  act["after_row"],
            })

    # Групуємо дії за cell_id для побудови undo_ops
    actions_by_cell: dict[str, list[dict]] = {}
    for act in actions:
        cid = act.get("_cell_id", "")
        actions_by_cell.setdefault(cid, []).append(act)

    snap_cells: list[dict] = []
    seen_snap: set = set()
    for key in sorted(touched_keys):
        t, r = key
        for col_name, idx_map in (
            ("### COPYWRITER", copywriter_index),
            ("### INTEGRATOR", integrator_index),
        ):
            pars = idx_map.get(key)
            if not pars:
                continue
            snap_key = (t, r, col_name)
            if snap_key in seen_snap:
                continue
            seen_snap.add(snap_key)
            sorted_pars = sorted(pars, key=lambda p: p["start"])
            section = (sorted_pars[0].get("section") or "").replace("###", "").strip()
            col = sorted_pars[0].get("col", 0)
            full_text = "\n".join(p["text"] for p in sorted_pars)
            cell_id = f"{t}_{r}_{section}" + ("_INT" if col_name == "### INTEGRATOR" else "")

            # Будуємо undo_ops — точні зворотні операції без повного перезапису.
            # CW і INT можуть мати однаковий _cell_id, тому фільтруємо за типом:
            # - CW: insert_text (вставка на початку)
            # - INT: append_to_cell (вставка в кінець)
            undo_ops: list[dict] = []
            # Для CW беремо всі дії з однойменним cell_id (через спільний _cell_id)
            # Для INT беремо тільки append_to_cell
            all_cid_acts = actions_by_cell.get(cell_id, [])
            if col_name == "### COPYWRITER":
                relevant_acts = [a for a in all_cid_acts if a["action"] == "insert_text"]
            else:
                # INT: append_to_cell або delete_range+insert_text
                relevant_acts = [a for a in all_cid_acts
                                  if a["action"] in ("append_to_cell", "delete_range")]

            has_delete = any(a["action"] == "delete_range" for a in relevant_acts)
            if not has_delete:
                for a in relevant_acts:
                    if a["action"] == "insert_text":
                        undo_ops.append({
                            "action":    "delete_range",
                            "index":     a["index"],
                            "end_index": a["index"] + len(a["text"]),
                        })
                    elif a["action"] == "append_to_cell":
                        undo_ops.append({
                            "action": "delete_suffix",
                            "table":  a["table"],
                            "row":    a["row"],
                            "col":    a["col"],
                            "length": len(a["text"]),
                        })
            # has_delete → undo_ops порожній → rollback_Gdocs.py робить повний full_replace

            snap_cells.append({
                "cell_id":       cell_id,
                "table":         t,
                "row":           r,
                "section":       section,
                "column":        col_name,
                "col":           col,
                "original_text": full_text,
                "undo_ops":      undo_ops,
            })

    snapshot_data = {
        "module":        MODULE,
        "document_id":   _INFO.get("document_id", ""),
        "inserted_rows": inserted_rows_snap,
        "cells":         snap_cells,
    }
    ROLLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROLLBACK_FILE.write_text(
        json.dumps(snapshot_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  Saved rollback snapshot → {ROLLBACK_FILE.name} ({len(snap_cells)} клітинок)")
