from claude_cli import clean_output


def test_plain_text_unchanged():
    assert clean_output("**Заголовок**\nтекст") == "**Заголовок**\nтекст"


def test_crlf_normalized():
    assert clean_output("рядок один\r\nрядок два\r\n") == "рядок один\nрядок два"


def test_cell_end_marker_cut_off():
    raw = "**Заголовок**\nтекст\n### CELL_END"
    assert clean_output(raw) == "**Заголовок**\nтекст"


def test_text_after_cell_end_discarded():
    raw = "текст\n### CELL_END\n\nЯ обробив клітинку і додав маркери."
    assert clean_output(raw) == "текст"


def test_fence_unwrapped():
    raw = "```\n**Заголовок**\nтекст\n```"
    assert clean_output(raw) == "**Заголовок**\nтекст"


def test_fence_with_language_unwrapped():
    raw = "```markdown\n**Заголовок**\n```"
    assert clean_output(raw) == "**Заголовок**"


def test_inner_fence_preserved():
    """Огорожа знімається лише коли обгортає ВЕСЬ вивід."""
    raw = "текст\n```\nкод\n```\nще текст"
    assert clean_output(raw) == "текст\n```\nкод\n```\nще текст"


def test_empty_output():
    assert clean_output("") == ""
    assert clean_output("   \n\n  ") == ""


def test_fence_and_cell_end_together():
    raw = "```\nтекст\n### CELL_END\n```"
    assert clean_output(raw) == "текст"


import claude_cli


def _reset_cache():
    claude_cli._exe_cache = None


def test_env_override_used(tmp_path, monkeypatch):
    fake = tmp_path / "claude.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(fake))
    _reset_cache()
    assert claude_cli.find_executable() == str(fake)
    _reset_cache()


def test_env_override_missing_is_not_found(monkeypatch):
    """Явно заданий, але неіснуючий шлях не підмінюється знахідкою з PATH."""
    monkeypatch.setenv("CLAUDE_CLI_PATH", "D:/немає/такого/claude.exe")
    _reset_cache()
    assert claude_cli.find_executable() is None
    _reset_cache()


def test_not_found_when_path_and_install_dirs_empty(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli, "_install_candidates", list)
    _reset_cache()
    assert claude_cli.find_executable() is None
    _reset_cache()


def test_falls_back_to_install_dir_when_path_stale(tmp_path, monkeypatch):
    """Стара PATH у терміналі VS Code — CLI все одно знаходиться."""
    fake = tmp_path / "claude.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli, "_install_candidates", lambda: [str(fake)])
    _reset_cache()
    assert claude_cli.find_executable() == str(fake)
    _reset_cache()
