import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
import json, pathlib, asyncio, argparse, os, re

import claude_cli

_INFO = json.loads((pathlib.Path(__file__).parent / "info.json").read_text(encoding="utf-8"))
DEFAULT_MODULE = _INFO["module"]

# --- аргументи командного рядка ---
parser = argparse.ArgumentParser()
parser.add_argument("module", nargs="?", default=DEFAULT_MODULE,
                    help=f"Назва модуля (папка в Text/). Default: {DEFAULT_MODULE}")
parser.add_argument("--training", metavar="LESSON",
                    help="Режим тренування: читає з training/<LESSON>/before/, пише у training/<LESSON>/gpt_out/")
parser.add_argument("--sections", default=None,
                    help="Comma-separated секції для обробки, напр. PRACTICA,APPENDIX. За замовчуванням — всі.")
parser.add_argument("--cell", default=None,
                    help="Обробити тільки одну клітинку: рядок (напр. 16) або СЕКЦІЯ:N (напр. PRACTICA:2)")
parser.add_argument("--backend", choices=["claude", "gemini"],
                    default=os.getenv("GS_BACKEND", "claude"),
                    help="Бекенд генерації: claude (Claude Code CLI, дефолт) або gemini (API).")
args = parser.parse_args()
BACKEND = args.backend
MODULE = args.module
SECTIONS_FILTER = set(s.strip().upper() for s in args.sections.split(",")) if args.sections else None

# ── ШЛЯХИ ───────────────────────────────────────────────────────────────────
ROOT       = pathlib.Path(__file__).parent
_subject_code = _INFO.get("subject_code") or MODULE.split()[0]
_subject_promts = ROOT / "prompts" / _subject_code
PROMPT_DIR = _subject_promts if _subject_promts.exists() else ROOT / "prompts"

TRAINING_MODE = bool(args.training)

if TRAINING_MODE:
    CELLS_DIR = ROOT / "training" / args.training / "before"
    OUT_DIR   = ROOT / "training" / args.training / "gpt_out"
else:
    CELLS_DIR = ROOT / "Text" / MODULE / "cells_out"
    OUT_DIR   = ROOT / "Text" / MODULE / "cells_gpt_out"

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── ПРОМПТИ ──────────────────────────────────────────────────────────────────
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")

# Спільна база (Global_promt.txt) — префікс до КОЖНОГО skill-промту, щоб базові
# правила маркерів діяли скрізь однаково (раніше Global не завантажувався взагалі).
GLOBAL_PROMPT = load_prompt("Global_promt.txt")
_SKILL_SEP = "\n\n──────────────────────────────────────────────\n\n"


def _skill(name: str) -> str:
    return GLOBAL_PROMPT + _SKILL_SEP + load_prompt(name)


SKILLS = {
    "theoria_structure": _skill("skill_theoria_structure.txt"),
    "theoria_images":    _skill("skill_theoria_images.txt"),
    "theoria_blocks":    _skill("skill_theoria_blocks.txt"),
    "practica":          _skill("skill_practica.txt"),
    "test":              _skill("skill_test.txt"),
    "appendix":          _skill("skill_appendix.txt"),
    "final":             _skill("skill_final.txt"),
}

# WHY-інструкція тільки у training-режимі
WHY_INSTRUCTION = """

Перед рядком ### CELL_END додай блок:
### WHY
- Для кожного доданого або НЕ доданого маркера — одне речення чому.
- Приклад: «Додав ** до "Біографія": рядок стоїть окремо, це заголовок.»
- Приклад: «Не додав [[INSERT:??]] після "...питання?": воно всередині абзацу.»
"""

# ── ІНІЦІАЛІЗАЦІЯ БЕКЕНДУ ────────────────────────────────────────────────────
load_dotenv()

GEMINI_MODEL = "gemini-3.5-flash"

# Per-cell паралельно: кожна клітинка — окремий виклик (якість: без батч-дропів і
# cross-contamination). Семафор дає швидкість, не впираючись у rate-limit.
# У claude-бекенді паралеляться ПРОЦЕСИ, а не HTTP-запити — тримаємо менше.
if BACKEND == "gemini":
    MODEL = GEMINI_MODEL
    SEM = asyncio.Semaphore(6)
else:
    MODEL = f"claude-cli:{claude_cli.MODEL}"
    SEM = asyncio.Semaphore(3)

_CLIENT = None


def _gemini_client():
    """Клієнт створюється лениво: без --backend gemini ключ узагалі не потрібен."""
    global _CLIENT
    if _CLIENT is None:
        from google import genai
        _CLIENT = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _CLIENT

# ── ВИКЛИКИ GEMINI API ───────────────────────────────────────────────────────
async def _call(system: str, user_content: str, max_tokens: int, stop: list[str] | None) -> str:
    from google.genai import types
    async with SEM:
        for attempt in range(5):
            try:
                response = await _gemini_client().aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=user_content,
                    config=types.GenerateContentConfig(
                        temperature=0,
                        system_instruction=system,
                        max_output_tokens=max_tokens,
                        stop_sequences=stop,
                    )
                )
                return response.text or ""
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    wait = 60 * (attempt + 1)
                    print(f"\n⏳ Rate limit, чекаємо {wait}с...", end="", flush=True)
                    await asyncio.sleep(wait)
                else:
                    raise
        raise Exception("Вичерпано всі спроби після rate limit")

async def call_model(system: str, user_content: str) -> str:
    """Один виклик моделі поточним бекендом. Семафор тримає кожна гілка сама."""
    if BACKEND == "gemini":
        # 8000 токенів — з запасом на велику клітинку (напр. TEST з кількома питаннями).
        return await _call(system, user_content, max_tokens=8000, stop=["### CELL_END"])
    async with SEM:
        return await claude_cli.call_claude_cli(system, user_content)


# ── ПРЕ-ПРОЦЕСИНГ: вставка H5P-тегу в INTEGRATOR ────────────────────────────
_INT_SECTION_RE = re.compile(r'(### INTEGRATOR\n)(.*?)(?=\n###|\Z)', re.DOTALL)
_H5P_TAG_RE     = re.compile(r'=(?:DT|FT|SCT|MCT|CW)=')

INTEGRATOR_TRIGGERS: dict[str, str] = {
    "робота в зошиті": "=FT=",
    # "перетягувати слова" — НЕ pre-inject: Gemini має сам генерувати h: + тіло (ВАРІАНТ В)
    "відповідності":      "=DT=",
    "послідовності":      "=DT=",
    "пари":               "=DT=",
}

def _inject_h5p_tag(content: str) -> str:
    """Якщо INTEGRATOR має відомий тригер, але без H5P-тегу — вставляє тег."""
    def _replace(m: re.Match) -> str:
        prefix, int_text = m.group(1), m.group(2).rstrip()
        if _H5P_TAG_RE.search(int_text):
            return m.group(0)  # тег вже є
        for trigger, tag in INTEGRATOR_TRIGGERS.items():
            if trigger in int_text.lower():
                return f"{prefix}{int_text}\n{tag}"
        return m.group(0)
    return _INT_SECTION_RE.sub(_replace, content)


_SCT_BLOCK_RE = re.compile(r'(=SCT=\nh:[^\n]*\n)(.*?)(?=\n###|\Z)', re.DOTALL)

def _fix_sct_spacing(content: str) -> str:
    """Додає порожній рядок між питаннями у =SCT= блоках."""
    def _fix_block(m: re.Match) -> str:
        header = m.group(1)   # "=SCT=\nh: ...\n"
        body   = m.group(2).rstrip()
        lines  = body.split("\n")
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Нове питання починається якщо рядок закінчується на ? або →
            # і попередній блок вже має хоча б одну відповідь (тобто out не порожній)
            is_question = stripped.endswith("?") or stripped.endswith("→")
            if is_question and out:
                out.append("")   # порожній рядок-розділювач
            out.append(line)
        return header + "\n".join(out)
    return _SCT_BLOCK_RE.sub(_fix_block, content)


# ── ПАЙПЛАЙН ПО СЕКЦІЯХ ──────────────────────────────────────────────────────
def get_section(file_path: pathlib.Path) -> str:
    """0_11_THEORIA.txt → THEORIA"""
    return file_path.stem.split("_")[-1].upper()


def _file_row(f: pathlib.Path) -> str:
    """0_16_PRACTICA.txt → '16'"""
    return f.stem.split("_")[1]


def filter_by_cell(files: list[pathlib.Path], spec: str, all_files: list[pathlib.Path]) -> list[pathlib.Path]:
    """
    Фільтрує files за --cell специфікацією.
    all_files — повний список для обчислення відносного індексу (СЕКЦІЯ:N).
    """
    spec = spec.strip()
    if ":" in spec:
        section, num_str = spec.upper().split(":", 1)
        n = int(num_str)
        sec_all = sorted(f for f in all_files if get_section(f) == section)
        if not (1 <= n <= len(sec_all)):
            return []
        target_row = _file_row(sec_all[n - 1])
        return [f for f in files if _file_row(f) == target_row]
    else:
        return [f for f in files if _file_row(f) == spec]


_THEORIA_FAMILY = ("THEORIA", "FACT", "EXAMPLES")


def needs_model(section: str) -> bool:
    """Секції без skill-промпту йдуть copy-through, без жодного виклику моделі."""
    return section in _THEORIA_FAMILY or section.lower() in SKILLS


async def process_theoria(content: str) -> str:
    result = await call_model(SKILLS["theoria_structure"], content)
    result = await call_model(SKILLS["theoria_blocks"],    result)
    return result

async def process_file(file_path: pathlib.Path) -> str:
    """Per-cell виклик для всіх секцій. Кожна клітинка — окремий запит до моделі.
    Copy-through: якщо модель повернула порожнє — повертаємо оригінал (нічого не губимо)."""
    section = get_section(file_path)
    content = file_path.read_text(encoding="utf-8")
    if section in _THEORIA_FAMILY:
        result = await process_theoria(content)
    else:
        skill_key = section.lower()
        if skill_key not in SKILLS:
            return content  # невідома секція — без змін
        system = SKILLS[skill_key]
        if TRAINING_MODE:
            system += WHY_INSTRUCTION
        result = await call_model(system, _inject_h5p_tag(content))
    return result if (result and result.strip()) else content


# ── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    all_files = sorted(CELLS_DIR.glob("*.txt"))
    if not all_files:
        print(f"❌ Немає файлів у {CELLS_DIR}")
        return
    files = all_files[:]

    if SECTIONS_FILTER:
        files = [f for f in files if get_section(f) in SECTIONS_FILTER]
        if not files:
            print(f"❌ Жоден файл не відповідає --sections {args.sections}")
            return
        print(f"🔍 Фільтр секцій: {SECTIONS_FILTER} → {len(files)} файлів")

    if args.cell:
        files = filter_by_cell(files, args.cell, all_files)
        if not files:
            print(f"❌ Жоден файл не відповідає --cell {args.cell}")
            return
        print(f"🔍 Фільтр клітинки: --cell {args.cell} → {[f.name for f in files]}")

    if BACKEND == "claude" and not claude_cli.find_executable():
        print(f"❌ {claude_cli.NOT_FOUND_HINT}")
        return 1

    mode_label = f"[TRAINING: {args.training}]" if TRAINING_MODE else ""
    print(f"📡 Обробка {len(files)} файлів | {MODEL} | per-cell паралельно (sem={SEM._value}) {mode_label}")

    # Кожна клітинка — окрема корутина (per-cell), усі йдуть паралельно під семафором.
    coros = [process_file(f) for f in files]
    raw = await asyncio.gather(*coros, return_exceptions=True)

    all_results: dict[str, str] = {}
    for f, result in zip(files, raw):
        if isinstance(result, Exception):
            # Copy-through: не губимо клітинку — кладемо оригінал, гучно попереджаємо.
            all_results[f.name] = f.read_text(encoding="utf-8")
            print(f"  {f.name} ... ⚠️  помилка ({result}) — лишаю оригінал")
        else:
            all_results[f.name] = result
            print(f"  {f.name} ... ✅")

    saved = 0
    for f in files:
        text = _fix_sct_spacing(all_results[f.name])
        (OUT_DIR / f.name).write_text(text, encoding="utf-8")
        saved += 1

    errors = sum(1 for r in raw if isinstance(r, Exception))
    model_cells = sum(1 for f in files if needs_model(get_section(f)))

    print(f"\n✅  Готово: {saved}/{len(files)} → {OUT_DIR}")

    if errors:
        print(f"⚠️  Клітинок з помилкою: {errors}/{model_cells} — там лишився оригінал")
    if model_cells and errors == model_cells:
        print("❌ Жодна клітинка не оброблена моделлю. Зупиняю пайплайн, щоб "
              "cells_to_actions/change_Gdocs не записали в документ порожню розмітку.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
