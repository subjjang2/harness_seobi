"""tdd_guard.py (Claude PreToolUse TDD 가드) 단위 테스트.

.claude/hooks/tdd_guard.py 를 경로 import 해, 면제 규칙과 deny 출력을 검증한다.
`pytest scripts/` 가 자동 수집하므로 execute.py 검증 게이트에도 포함된다.
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

# .claude/hooks/tdd_guard.py 를 파일 경로로 로드(패키지 아님).
_GUARD_PATH = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "tdd_guard.py"
_spec = importlib.util.spec_from_file_location("tdd_guard", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _run(file_path, monkeypatch, cwd=None):
    """주어진 file_path 페이로드로 guard.main() 을 돌리고 (exit_code, stdout) 반환."""
    payload = json.dumps({"tool_input": {"file_path": file_path}}).encode("utf-8")
    monkeypatch.setattr(guard.sys, "stdin", type("S", (), {"buffer": io.BytesIO(payload)})())
    buf = io.BytesIO()
    monkeypatch.setattr(guard.sys, "stdout", type("O", (), {"buffer": buf})())
    if cwd:
        monkeypatch.chdir(cwd)
    code = guard.main()
    return code, buf.getvalue().decode("utf-8")


def test_no_file_path_passes(monkeypatch):
    payload = json.dumps({"tool_input": {}}).encode("utf-8")
    monkeypatch.setattr(guard.sys, "stdin", type("S", (), {"buffer": io.BytesIO(payload)})())
    buf = io.BytesIO()
    monkeypatch.setattr(guard.sys, "stdout", type("O", (), {"buffer": buf})())
    assert guard.main() == 0
    assert buf.getvalue() == b""


def test_empty_stdin_passes(monkeypatch):
    monkeypatch.setattr(guard.sys, "stdin", type("S", (), {"buffer": io.BytesIO(b"")})())
    buf = io.BytesIO()
    monkeypatch.setattr(guard.sys, "stdout", type("O", (), {"buffer": buf})())
    assert guard.main() == 0
    assert buf.getvalue() == b""


def test_non_python_passes(monkeypatch):
    code, out = _run("scripts/README.md", monkeypatch)
    assert code == 0 and out == ""


@pytest.mark.parametrize(
    "path",
    [
        "scripts/test_something.py",       # test_*.py
        "scripts/something_test.py",       # *_test.py
        "scripts/conftest.py",
        "scripts/__init__.py",
        ".claude/hooks/tdd_guard.py",      # .claude/ 면제
        ".claude/skills/x/scripts/foo.py",
        "docs/example.py",                 # docs/ 면제
        "tests/helper.py",                 # tests/ 면제
    ],
)
def test_exempt_paths_pass(path, monkeypatch):
    code, out = _run(path, monkeypatch)
    assert code == 0 and out == "", f"{path} 는 면제되어야 함"


def test_source_with_test_passes(tmp_path, monkeypatch):
    (tmp_path / "mymod.py").write_text("x = 1")
    (tmp_path / "test_mymod.py").write_text("def test_x(): assert True")
    code, out = _run(str(tmp_path / "mymod.py"), monkeypatch)
    assert code == 0 and out == ""


def test_source_with_sibling_style_test_passes(tmp_path, monkeypatch):
    (tmp_path / "mymod.py").write_text("x = 1")
    (tmp_path / "mymod_test.py").write_text("def test_x(): assert True")
    code, out = _run(str(tmp_path / "mymod.py"), monkeypatch)
    assert code == 0 and out == ""


def test_source_without_test_denies(tmp_path, monkeypatch):
    (tmp_path / "newmod.py").write_text("def f(): return 1")
    code, out = _run(str(tmp_path / "newmod.py"), monkeypatch)
    assert code == 0
    decision = json.loads(out)
    hso = decision["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "newmod" in hso["permissionDecisionReason"]


def test_windows_backslash_path_normalized(tmp_path, monkeypatch):
    # 백슬래시 경로에서도 .claude 면제가 걸려야 한다.
    code, out = _run(r".claude\hooks\some_tool.py", monkeypatch)
    assert code == 0 and out == ""


def test_tests_subdir_test_passes(tmp_path, monkeypatch):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "mod.py").write_text("x = 1")
    tdir = src / "tests"
    tdir.mkdir()
    (tdir / "test_mod.py").write_text("def test_x(): assert True")
    code, out = _run(str(src / "mod.py"), monkeypatch)
    assert code == 0 and out == ""
