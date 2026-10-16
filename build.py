from __future__ import annotations

import json
import argparse
import subprocess
import sys
import os
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


WORKDIR = Path(__file__).resolve().parent
# Optional: path to the moodle-lesson-pipeline checkout. When set, the lesson
# settings are mirrored into its info/info.json and its checkpoints are reset
# after a successful markup run, so the two tools stay in step.
_pipeline_env = os.environ.get("LESSON_PIPELINE_DIR", "")
PIPELINE_DIR = Path(_pipeline_env) if _pipeline_env else None
INFO_JSON = WORKDIR / "info.json"
PIPELINE_INFO_JSON = PIPELINE_DIR / "info" / "info.json" if PIPELINE_DIR else None
CHECKPOINT_DIR = PIPELINE_DIR / "info" / "pipeline_checkpoints" if PIPELINE_DIR else None


SECTIONS = [
    "THEORIA",
    "PRACTICA",
    "TEST",
    "APPENDIX",
]
DEFAULT_SECTIONS = [
    "THEORIA",
    "DICTIONARY",   # callout «!!» для словника (Запам'ятай! → !!) — детерміновано у cells_to_actions
    "PRACTICA",
    # "TEST",       # ВИМКНЕНО: Gemini переписував TEST-клітинки лише заради нумерації
                    # «N. », а перезапис (delete+insert+reset style) стирав зелену
                    # заливку правильної відповіді → тест у Moodle без правильних
                    # відповідей. Номер тепер береться з 1-ї колонки таблиці
                    # безпосередньо в doc_to_html, тож чіпати документ не треба.
    "APPENDIX",
    "INTERACTIVE",  # своп назва↔<iframe> (назва перед iframe) — детерміновано у cells_to_actions
    "FINAL",
]


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


def sync_to_pipeline_info() -> None:
    if PIPELINE_INFO_JSON is None:
        return
    local = read_json(INFO_JSON)
    pipeline = read_json(PIPELINE_INFO_JSON) if PIPELINE_INFO_JSON.exists() else {}

    pipeline["code_subject"] = local["module"]
    pipeline["document_id"] = local["document_id"]
    pipeline["credentials"] = local["credentials"]
    if local.get("current_subject"):
        pipeline["current_subject"] = local["current_subject"]

    write_json(PIPELINE_INFO_JSON, pipeline)
    print(f"[generate_symbols] synced info: {local['module']}")


def run_command(command: list[str]) -> None:
    print(f"\n[generate_symbols] run: {' '.join(command)}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run(command, cwd=WORKDIR, check=True, env=env)


def lesson_code() -> str:
    return read_json(INFO_JSON)["module"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sections",
        default=",".join(DEFAULT_SECTIONS),
        help="Comma-separated sections for GPT/actions, e.g. PRACTICA or PRACTICA,TEST.",
    )
    parser.add_argument(
        "--cell",
        default=None,
        help="Only one row/cell, e.g. 19, or section-relative value, e.g. PRACTICA:2.",
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Лише генерація розмітки (generate_symbol + create_user_input + gpt_async), "
             "БЕЗ cells_to_actions/change_Gdocs. Для оркестратора: запис у doc — окремий крок apply_changes.",
    )
    parser.add_argument(
        "--backend",
        choices=["claude", "gemini"],
        default=os.getenv("GS_BACKEND", "claude"),
        help="Бекенд розмітки для gpt_async: claude (Claude Code CLI, дефолт) або gemini (API).",
    )
    return parser.parse_args()


def generator_command(script: str, code: str, sections: list[str], cell: str | None) -> list[str]:
    command = [sys.executable, script, code]
    if sections:
        command += ["--sections", ",".join(sections)]
    if cell:
        command += ["--cell", cell]
    return command


def reset_moodle_checkpoints() -> None:
    if CHECKPOINT_DIR is None or not CHECKPOINT_DIR.exists():
        return

    for file in CHECKPOINT_DIR.iterdir():
        if file.is_file():
            file.unlink()
    print("[generate_symbols] Moodle pipeline checkpoints reset")


def main() -> int:
    args = parse_args()
    sections = [item.strip().upper() for item in args.sections.split(",") if item.strip()]

    sync_to_pipeline_info()
    code = lesson_code()

    commands = [
        [sys.executable, "generate_symbol.py"],
        [sys.executable, "create_user_input.py"],
        generator_command("gpt_async.py", code, sections, args.cell)
        + ["--backend", args.backend],
    ]
    if not args.generate_only:
        commands += [
            generator_command("cells_to_actions.py", code, sections, args.cell),
            [sys.executable, "change_Gdocs.py"],
        ]

    for command in commands:
        run_command(command)

    if args.generate_only:
        print("\n[generate_symbols] generate-only: розмітку згенеровано, "
              "doc НЕ змінено (cells_to_actions/change_Gdocs пропущено)")
        return 0

    reset_moodle_checkpoints()
    print("\n[generate_symbols] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
