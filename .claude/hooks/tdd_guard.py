#!/usr/bin/env python3
"""TDD Guard Hook — Claude Code PreToolUse[Edit|Write].

테스트 없이 Python 구현 코드를 편집/작성하려 하면 deny 로 차단한다.
데모 repo 의 .claude/hooks/tdd-guard.sh(TS/Next.js 전용)를 이 프로젝트의
언어/컨벤션(Python + pytest `test_<name>.py`)에 맞게 포팅한 것.

한계: Claude Code 훅은 harness 의 `codex exec` 세션에선 발동하지 않는다(README 참조).
즉 이 가드는 사람/Claude 가 대화형 Claude Code 로 편집할 때만 걸린다. codex 실행
경로의 실제 강제는 execute.py 검증 게이트(pytest)가 담당한다.

입력: stdin 으로 PreToolUse JSON (`tool_input.file_path`).
출력: 테스트 부재 시 permissionDecision:"deny" JSON. 그 외에는 무출력 + exit 0.
설계상 fail-open: 입력이 없거나 손상되면 통과시킨다(편집 자체를 막지 않음).
"""

import json
import os
import re
import sys
from pathlib import Path

# 테스트 불필요 — 파일명 자체가 면제 대상인 것들
EXEMPT_BASENAMES = {"conftest.py", "__init__.py", "setup.py"}
# 테스트 비대상 디렉토리(경로에 이 세그먼트가 있으면 면제)
EXEMPT_DIR_SEGMENTS = (".claude", "docs", "tests")


def _emit_deny(name: str) -> None:
    """Claude PreToolUse deny 계약으로 차단 사유를 출력한다(UTF-8 bytes)."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"TDD GUARD: '{name}'에 대한 테스트가 없습니다. 구현 전에 테스트를 "
                f"먼저 작성하세요 (예: test_{name}.py)."
            ),
        }
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # Windows cp949 stdout 에서 한글이 깨지거나 UnicodeEncodeError 나는 것을 피하려
    # 텍스트 인코딩을 거치지 않고 바이트로 직접 쓴다.
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _is_exempt(norm_path: str, basename: str) -> bool:
    """테스트 요구에서 면제되는 경로인지 판정한다."""
    if not norm_path.endswith(".py"):
        return True
    if basename in EXEMPT_BASENAMES:
        return True
    # test_*.py / *_test.py (테스트 파일 자체)
    if basename.startswith("test_") or basename.endswith("_test.py"):
        return True
    # 경로 세그먼트 기반 면제(.claude/·docs/·tests/)
    segments = norm_path.split("/")
    if any(seg in EXEMPT_DIR_SEGMENTS for seg in segments):
        return True
    return False


def _git_root(start: Path) -> Path:
    """start 기준 git 최상위. 실패 시 cwd."""
    import subprocess

    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except Exception:
        pass
    return Path.cwd()


def _has_test(file_path: str) -> bool:
    """이 소스에 대응하는 테스트가 하나라도 존재하는지 확인한다."""
    p = Path(file_path)
    directory = p.parent
    name = p.stem  # 확장자 제외 파일명

    # (1) 같은 폴더: test_<name>.py / <name>_test.py
    if (directory / f"test_{name}.py").is_file():
        return True
    if (directory / f"{name}_test.py").is_file():
        return True
    # (2) 같은 폴더의 tests/ 하위
    if (directory / "tests" / f"test_{name}.py").is_file():
        return True
    # (3) 레포 tests/ 에서 이 모듈을 import 하는 테스트
    root = _git_root(directory if directory.exists() else Path.cwd())
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        pattern = re.compile(rf"(?:^|from |import ).*\b{re.escape(name)}\b")
        for tf in tests_dir.rglob("test_*.py"):
            try:
                if pattern.search(tf.read_text(encoding="utf-8", errors="ignore")):
                    return True
            except OSError:
                continue
    return False


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw:
        return 0
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return 0  # 손상 입력 → fail-open

    file_path = (data.get("tool_input") or {}).get("file_path")
    if not file_path:
        return 0

    # Windows 경로(백슬래시)를 forward slash 로 정규화한다.
    norm = str(file_path).replace("\\", "/")
    basename = norm.rsplit("/", 1)[-1]

    if _is_exempt(norm, basename):
        return 0

    if not _has_test(norm):
        _emit_deny(Path(norm).stem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
