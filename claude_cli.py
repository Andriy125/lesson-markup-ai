"""Дублюючий бекенд генерації: виклик моделі через Claude Code CLI (`claude -p`).

Потрібен, коли Gemini API недоступний (не оплачений). Кожна клітинка — окремий
процес `claude -p`, паралельність тримає семафор у gpt_async.py.

Промпт іде через stdin, а не argv: skill-промпти сягають 28 КБ, а Windows має
ліміт командного рядка 32767 символів.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import tempfile

# Модель за замовчуванням — sonnet (баланс якість/ліміти підписки).
MODEL = os.getenv("CLAUDE_CLI_MODEL", "sonnet")
TIMEOUT = int(os.getenv("CLAUDE_CLI_TIMEOUT", "300"))
RETRIES = int(os.getenv("CLAUDE_CLI_RETRIES", "3"))

# --system-prompt ПОВНІСТЮ замінює системний промпт Claude Code: агентська
# персона зникає, лишається чистий виклик моделі.
PROCESSOR_SYSTEM = (
    "Ти — детермінований текстовий процесор. Тобі дають інструкцію з розмітки "
    "і текст клітинки. Виконай інструкцію буквально і виведи ЛИШЕ готовий текст "
    "клітинки: без пояснень, без коментарів, без markdown-огорожі, без виклику "
    "інструментів. Не додавай і не прибирай нічого, крім того, що прямо вимагає "
    "інструкція."
)

SEPARATOR = "\n\n════════════ ТЕКСТ КЛІТИНКИ ════════════\n\n"

_CELL_END = "### CELL_END"
_FENCE_RE = re.compile(r"\A```[^\n]*\n(.*)\n```\Z", re.DOTALL)

# Ці змінні лишає по собі батьківська сесія Claude Code — прибираємо, щоб
# дочірній процес не вважав себе вкладеним.
_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

_work_dir: pathlib.Path | None = None
_exe_cache: str | None = None


def build_stdin(system: str, user_content: str) -> str:
    """Skill-промпт + вміст клітинки в одне повідомлення користувача."""
    return system + SEPARATOR + user_content


def clean_output(raw: str) -> str:
    """Нормалізує відповідь CLI до того ж вигляду, що давав Gemini.

    Обрізає все від `### CELL_END` (Gemini робив це через stop_sequences),
    знімає markdown-огорожу, якщо модель обгорнула весь вивід.
    """
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    idx = text.find(_CELL_END)
    if idx != -1:
        text = text[:idx].rstrip()
    return text


def _install_candidates() -> list[str]:
    """Типові місця встановлення — на випадок застарілого PATH у терміналі.

    VS Code успадковує PATH від сесії, у якій його запустили: якщо CLI поставили
    пізніше, `claude` у PATH не видно, доки термінал (чи сам VS Code) не
    перезапустять. Тому PATH — не єдине джерело правди.
    """
    home = os.path.expanduser("~")
    appdata = os.getenv("APPDATA") or os.path.join(home, "AppData", "Roaming")
    localappdata = os.getenv("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    return [
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(appdata, "npm", "claude.cmd"),
        os.path.join(localappdata, "Programs", "claude", "claude.exe"),
    ]


def find_executable() -> str | None:
    """Шлях до `claude` або None. Результат кешується на процес."""
    global _exe_cache
    if _exe_cache:
        return _exe_cache

    override = os.getenv("CLAUDE_CLI_PATH")
    if override:
        # Явно заданий шлях не має тихо підмінятися знайденим у PATH: краще
        # гучно впасти, ніж запустити не той бінарник.
        _exe_cache = override if os.path.isfile(override) else None
        return _exe_cache

    found = shutil.which("claude")
    if not found:
        found = next((c for c in _install_candidates() if os.path.isfile(c)), None)

    _exe_cache = found
    return _exe_cache


NOT_FOUND_HINT = """Не знайдено `claude` CLI.
  • Найчастіша причина: термінал VS Code успадкував стару PATH — перезапусти
    VS Code повністю (не лише термінал).
  • Або вкажи шлях явно: змінна оточення CLAUDE_CLI_PATH.
  • Або запусти з --backend gemini."""


def _executable() -> str:
    exe = find_executable()
    if not exe:
        raise RuntimeError(NOT_FOUND_HINT)
    return exe


def _cwd() -> pathlib.Path:
    """Чиста тимчасова тека: щоб CLI не підхоплював CLAUDE.md проєкту."""
    global _work_dir
    if _work_dir is None:
        _work_dir = pathlib.Path(tempfile.mkdtemp(prefix="gs_claude_cli_"))
    return _work_dir


def _env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _STRIP_ENV:
        env.pop(key, None)
    return env


def _command() -> list[str]:
    return [
        _executable(),
        "-p",
        "--model", MODEL,
        "--system-prompt", PROCESSOR_SYSTEM,
        "--output-format", "json",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disallowed-tools",
        "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite",
    ]


def _parse_response(stdout: str) -> tuple[str, str]:
    """(текст, помилка). --output-format json завжди дає один JSON-об'єкт."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Не JSON — беремо як є, краще щось, ніж нічого.
        return stdout, ""
    if data.get("is_error"):
        return "", str(data.get("result") or data.get("subtype") or "is_error")
    return str(data.get("result") or ""), ""


async def _run_once(payload: str) -> tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_cwd()),
        env=_env(),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(payload.encode("utf-8")), timeout=TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        proc.returncode,
    )


async def call_claude_cli(system: str, user_content: str) -> str:
    """Один виклик моделі через CLI. Кидає RuntimeError, якщо всі спроби провалились."""
    payload = build_stdin(system, user_content)
    last_err = ""

    for attempt in range(RETRIES):
        try:
            stdout, stderr, code = await _run_once(payload)
        except asyncio.TimeoutError:
            last_err = f"таймаут {TIMEOUT}с"
        else:
            if code == 0:
                result, err = _parse_response(stdout)
                text = clean_output(result)
                if text:
                    return text
                last_err = err or f"порожній вивід; stderr: {stderr.strip()[:200]}"
            else:
                last_err = f"exit {code}: {(stderr or stdout).strip()[:300]}"

        if attempt < RETRIES - 1:
            await asyncio.sleep(3 * (attempt + 1))

    raise RuntimeError(f"claude CLI: {RETRIES} спроби невдалі — {last_err}")
