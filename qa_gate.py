# qa_gate.py — детермінований шар перевірок («зверни увагу») для пайплайну.
# Без LLM: читає артефакти й звіряє з декларативними правилами. Те, що ловив би
# агент-супровід, але дешево й надійно. LLM лишається тільки на семантику окремо.
#
# Запуск:  PYTHONIOENCODING=utf-8 python qa_gate.py [module]
#   module — папка в Text/ (default: info.json["module"])
# Exit code: 0 — чисто або лише WARN; 1 — є ERROR (зламає урок у Moodle).

import sys, json, re, pathlib, argparse
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).parent
INFO = json.loads((ROOT / "info.json").read_text(encoding="utf-8"))

ap = argparse.ArgumentParser()
ap.add_argument("module", nargs="?", default=INFO["module"])
ap.add_argument("--json", action="store_true", help="Машинний вивід для агента-оркестратора")
_args = ap.parse_args()
MODULE = _args.module

CELLS_OUT = ROOT / "Text" / MODULE / "cells_out"
GPT_OUT   = ROOT / "Text" / MODULE / "cells_gpt_out"
RAW_FILE  = ROOT / "lesson_raw.json"

# ── ДЕКЛАРАТИВНІ ПРАВИЛА (один набір — і для пайплайну, і для перевірок) ──────
H5P_TAG_RE = re.compile(r'=(?:FT|DT|SCT|MCT|CW)=')
FT_TRIGGERS = ["пропуски", "вставте", "впиши", "вписати", "підбери", "робота в зошиті", "написати"]
DT_TRIGGERS = ["відповідності", "пари", "з'єднай", "поєднати", "послідовності", "порядку", "перетягувати слова"]
ALL_TRIGGERS = FT_TRIGGERS + DT_TRIGGERS
# Підказки «це закрите завдання» у COPYWRITER (для практик із порожнім INTEGRATOR).
CLOSED_TASK_HINTS = ALL_TRIGGERS + ["вибери", "обери", "познач", "доповни", "переклад"]

findings = []  # (severity, code, msg)
def err(code, msg):  findings.append(("ERROR", code, msg))
def warn(code, msg): findings.append(("WARN", code, msg))

# ── helpers ──────────────────────────────────────────────────────────────────
def read_section(text, name):
    """Витягує тіло блоку ### NAME до наступного ### або кінця."""
    m = re.search(rf'###\s*{name}\s*\n(.*?)(?=\n###|\Z)', text, re.DOTALL)
    return (m.group(1).strip() if m else "")

def strip_cr(s): return s.replace("⏎", "").rstrip()

# ── 1. TEST: кожне питання має рівно одне == (правильна відповідь) ────────────
def check_tests():
    for f in sorted(GPT_OUT.glob("*_TEST.txt")):
        cw = read_section(f.read_text(encoding="utf-8"), "COPYWRITER")
        lines = [strip_cr(l) for l in cw.split("\n")]
        q_idx, q_text, marks = None, "", 0
        def flush():
            if q_idx is None: return
            multi = "всі правильн" in q_text.lower() or "усі правильн" in q_text.lower()
            if marks == 0:
                err("TEST_NO_ANSWER", f"{f.name}: питання «{q_text[:45]}» БЕЗ правильної відповіді (нема ==)")
            elif marks > 1 and not multi:
                warn("TEST_MULTI", f"{f.name}: питання «{q_text[:45]}» має {marks} == (single-choice?)")
        for l in lines:
            if re.match(r'^\d+[.)]', l.strip()):
                flush(); q_idx, q_text, marks = l, l, l.count("==") // 2
            elif q_idx is not None:
                marks += l.count("==") // 2
        flush()

# ── 2. PRACTICA: є тригер/тег → мусить бути H5P-тег у INTEGRATOR ──────────────
def check_practica():
    for f in sorted(GPT_OUT.glob("*_PRACTICA.txt")):
        text = f.read_text(encoding="utf-8")
        integ = read_section(text, "INTEGRATOR")
        if not integ.strip():
            # Порожній INTEGRATOR, але завдання закрите (є «Відповіді:») → можливо
            # вчитель не дав підказки і H5P не згенеровано (треба глянути око).
            cw = read_section(text, "COPYWRITER").lower()
            if cw.strip() and ("відповід" in cw or any(h in cw for h in CLOSED_TASK_HINTS)):
                warn("PRACT_MAYBE_MISSED", f"{f.name}: закрите завдання, але INTEGRATOR порожній — H5P не згенеровано")
            continue
        m = H5P_TAG_RE.search(integ)                     # теги завжди великими — без .lower()
        hit = next((t for t in ALL_TRIGGERS if t in integ.lower()), None)
        if hit and not m:
            err("PRACT_NO_H5P", f"{f.name}: INTEGRATOR має підказку «{hit}», але H5P-тег не згенеровано")
        elif m and not integ[m.end():].strip():
            err("PRACT_EMPTY_H5P", f"{f.name}: H5P-тег {m.group()} БЕЗ тіла (порожній інтерактив)")

# ── 3. Copy-through: gpt_out == cells_out для секцій, що мають розмічатись ────
def check_copythrough():
    for f in sorted(GPT_OUT.glob("*.txt")):
        sec = f.stem.split("_")[-1].upper()
        if sec not in ("PRACTICA", "TEST"):
            continue
        src = CELLS_OUT / f.name
        if not src.exists():
            continue
        a = re.sub(r'\s+', ' ', src.read_text(encoding="utf-8")).strip()
        b = re.sub(r'\s+', ' ', f.read_text(encoding="utf-8")).strip()
        if a == b:
            warn("COPY_THROUGH", f"{f.name}: вихід ідентичний входу — Gemini нічого не змінив")

# ── 4. INTERACTIVE: кожен рядок-блок має і iframe, і назву ────────────────────
def check_interactive(raw):
    rows = {}
    for c in raw:
        if c.get("kind") != "table_cell": continue
        if (c.get("section") or "").replace("###", "").strip() != "INTERACTIVE": continue
        rows.setdefault(c["row"], []).append(c.get("text") or "")
    for r, texts in rows.items():
        joined = " ".join(texts)
        has_iframe = "iframe" in joined.lower()
        has_title  = any(t.strip() and "iframe" not in t.lower() and "http" not in t.lower()
                         and re.search(r'[A-Za-zА-Яа-яÄÖÜäöüß]', t) for t in texts)
        if has_iframe and not has_title:
            warn("INT_NO_TITLE", f"INTERACTIVE row {r}: є iframe, але нема назви блоку")

# ── 5. Покриття секцій: усе з lesson_raw має оброблятись пайплайном ──────────
HANDLED = {"GREETING","REVISION","THEORIA","FACT","EXAMPLES","PRACTICA",
           "INTERACTIVE","DICTIONARY","APPENDIX","TEST","REACTION","FINAL"}
def check_sections(raw):
    present = {(c.get("section") or "").replace("###","").strip()
              for c in raw if c.get("kind") == "table_cell"}
    present.discard("")
    for s in sorted(present - HANDLED):
        warn("SECTION_UNHANDLED", f"секція «{s}» є в lesson_raw, але не в списку оброблюваних")

# ── 6. Конфіг створення (section у URL) ──────────────────────────────────────
def check_config():
    routes_f = ROOT.parent / "config" / "subject_routes.json"
    if not routes_f.exists(): return
    routes = json.loads(routes_f.read_text(encoding="utf-8"))
    key = re.sub(r"[\d_].*$", "", MODULE.split()[0]) + re.sub(r"\D", "", MODULE.split()[0])
    route = routes.get(MODULE.split()[0]) or routes.get(key)
    if not route: return
    for fld in ("create_page_url", "create_quiz_url"):
        u = route.get(fld, "")
        if u and "section=" not in u:
            warn("CFG_NO_SECTION", f"{fld} без параметра section=")

# ── RUN ──────────────────────────────────────────────────────────────────────
def main():
    if not GPT_OUT.exists():
        print(f"❌ Нема {GPT_OUT} — спершу прожени пайплайн для «{MODULE}»")
        return 1
    raw = json.loads(RAW_FILE.read_text(encoding="utf-8")) if RAW_FILE.exists() else []
    check_tests(); check_practica(); check_copythrough()
    check_interactive(raw); check_sections(raw); check_config()

    errs = [f for f in findings if f[0] == "ERROR"]
    warns = [f for f in findings if f[0] == "WARN"]

    if _args.json:
        print(json.dumps({
            "module": MODULE,
            "ok": not errs,
            "errors": len(errs), "warnings": len(warns),
            "findings": [{"severity": s, "code": c, "msg": m} for s, c, m in findings],
        }, ensure_ascii=False))
        return 1 if errs else 0

    print(f"\n🔎 QA-гейт для «{MODULE}»: {len(errs)} ERROR, {len(warns)} WARN\n")
    for sev, code, msg in errs + warns:
        icon = "⛔" if sev == "ERROR" else "⚠️ "
        print(f"  {icon} [{code}] {msg}")
    if not findings:
        print("  ✅ чисто")
    print()
    return 1 if errs else 0

if __name__ == "__main__":
    raise SystemExit(main())
