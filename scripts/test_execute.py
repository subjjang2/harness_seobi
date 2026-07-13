"""
execute.py 리팩터링 안전망 테스트.
리팩터링 전후 동작이 동일한지 검증한다.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.", encoding="utf-8")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2))
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "0-mvp"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text(encoding="utf-8")
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text(encoding="utf-8")
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------

class TestLoadGuardrails:
    def test_loads_claude_md_and_docs(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_claude_md(self, executor, tmp_project):
        (tmp_project / "CLAUDE.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "CLAUDE.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        import shutil
        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        with patch.object(ex, "ROOT", tmp_path):
            # executor가 필요 없는 static-like 동작이므로 임시 인스턴스
            phases_dir = tmp_path / "phases" / "dummy"
            phases_dir.mkdir(parents=True)
            idx = {"project": "T", "phase": "t", "steps": []}
            (phases_dir / "index.json").write_text(json.dumps(idx), encoding="utf-8")
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    def test_includes_completed_with_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "이전 Step 산출물" in result

    def test_forbids_codex_commit(self, executor):
        # codex 에게 커밋을 지시하면 안 된다 — 커밋은 harness 전담.
        result = executor._build_preamble("", "")
        assert "git 커밋" in result
        assert "하지 마라" in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("", "")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("", "")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", "", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result

    def test_includes_max_retries(self, executor):
        result = executor._build_preamble("", "")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_instructs_verdict_reporting_not_index_write(self, executor):
        # F10c: codex 는 index.json 을 직접 쓰지 않고 HARNESS_STATUS 로 결과만 보고한다.
        result = executor._build_preamble("", "")
        assert "HARNESS_STATUS" in result
        assert "직접 수정하지 마라" in result


# ---------------------------------------------------------------------------
# _preflight_clean_worktree / _normalize_codex_commits (커밋 무결성)
# ---------------------------------------------------------------------------

class TestCommitIntegrity:
    def _git(self, executor, rc=0, stdout="", stderr=""):
        return MagicMock(returncode=rc, stdout=stdout, stderr=stderr)

    def test_clean_worktree_passes(self, executor):
        with patch.object(executor, "_run_git", return_value=self._git(executor, stdout="")):
            executor._preflight_clean_worktree()  # 예외 없음

    def test_only_phases_dirty_passes(self, executor):
        porcelain = " M phases/0-mvp/index.json\n?? phases/0-mvp/step2-output.json\n"
        with patch.object(executor, "_run_git", return_value=self._git(executor, stdout=porcelain)):
            executor._preflight_clean_worktree()  # phases/ 는 허용

    def test_dirty_outside_phases_exits(self, executor):
        porcelain = " M src/app.py\n M phases/0-mvp/index.json\n"
        with patch.object(executor, "_run_git", return_value=self._git(executor, stdout=porcelain)):
            with pytest.raises(SystemExit) as exc_info:
                executor._preflight_clean_worktree()
        assert exc_info.value.code == 1

    def test_normalize_soft_resets_when_codex_committed(self, executor):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("rev-parse", "HEAD"):
                return self._git(executor, stdout="newsha")
            return self._git(executor, rc=0)

        with patch.object(executor, "_run_git", side_effect=fake_git):
            executor._normalize_codex_commits("oldsha")

        assert ("reset", "--soft", "oldsha") in calls

    def test_normalize_noop_when_head_unchanged(self, executor):
        calls = []

        def fake_git(*args):
            calls.append(args)
            return self._git(executor, stdout="samesha")

        with patch.object(executor, "_run_git", side_effect=fake_git):
            executor._normalize_codex_commits("samesha")

        assert not any(a[:1] == ("reset",) for a in calls)

    def test_normalize_noop_when_no_pre_head(self, executor):
        with patch.object(executor, "_run_git") as mock_git:
            executor._normalize_codex_commits(None)
        mock_git.assert_not_called()


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        polish = next(p for p in data["phases"] if p["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text(encoding="utf-8"))
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text(encoding="utf-8"))
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    def test_two_phase_commit(self, executor):
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]

    def test_resets_top_index_from_feat(self, executor):
        # top index(phases/index.json)는 메타데이터라 feat 에서 제외(reset)되어야 한다.
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        resets = [c[3] for c in calls if c[0] == "reset"]
        assert "phases/index.json" in resets

    def test_commit_failure_is_fatal(self, executor):
        # git commit 실패는 WARN 이 아니라 harness 실패(exit 1)로 처리된다.
        def fake_git(*args):
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            if args[0] == "commit":
                return MagicMock(returncode=1, stderr="commit boom")
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        with pytest.raises(SystemExit) as exc_info:
            executor._commit_step(2, "ui")
        assert exc_info.value.code == 1

    def test_staging_failure_is_fatal(self, executor):
        # F10b: git add/reset 실패는 커밋 경계를 깨뜨리므로 harness 실패(exit 1)로 처리된다.
        def fake_git(*args):
            if args[0] == "reset":
                return MagicMock(returncode=1, stderr="reset boom")
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        with pytest.raises(SystemExit) as exc_info:
            executor._commit_step(2, "ui")
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# F10c-r: codex 의 상태 파일 직접 수정 무효화 (스냅샷 복원)
# ---------------------------------------------------------------------------

class TestStateFileRestore:
    def _setup(self, executor, tmp_path, idx_text, top_text):
        idx = tmp_path / "index.json"
        top = tmp_path / "top.json"
        idx.write_text(idx_text, encoding="utf-8")
        if top_text is not None:
            top.write_text(top_text, encoding="utf-8")
        executor._index_file = idx
        executor._top_index_file = top
        return idx, top

    def test_restore_reverts_codex_index_change(self, executor, tmp_path):
        idx, _ = self._setup(executor, tmp_path, '{"status": "pending"}', "[]")
        snap = executor._snapshot_state_files()
        idx.write_text('{"status": "completed", "injected": true}', encoding="utf-8")  # codex 가 몰래 씀
        executor._restore_state_files(snap)
        assert idx.read_text(encoding="utf-8") == '{"status": "pending"}'

    def test_restore_removes_codex_created_file(self, executor, tmp_path):
        _, top = self._setup(executor, tmp_path, "{}", None)  # top 은 처음에 없음
        snap = executor._snapshot_state_files()
        top.write_text('{"evil": 1}', encoding="utf-8")  # codex 가 새로 만듦
        executor._restore_state_files(snap)
        assert not top.exists()

    def test_restore_noop_when_unchanged(self, executor, tmp_path):
        idx, top = self._setup(executor, tmp_path, "{}", "[]")
        snap = executor._snapshot_state_files()
        executor._restore_state_files(snap)  # 변경 없음 → 그대로 유지
        assert idx.read_text(encoding="utf-8") == "{}"
        assert top.read_text(encoding="utf-8") == "[]"


# ---------------------------------------------------------------------------
# _invoke_codex (mocked)
# ---------------------------------------------------------------------------

class TestInvokeCodex:
    def test_invokes_codex_with_correct_args(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch.object(executor, "_run_tree", return_value=mock_result) as mock_run:
            output = executor._invoke_codex(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "workspace-write" in cmd  # 샌드박스로 워크스페이스 밖 쓰기 차단
        assert "sandbox_workspace_write.network_access=true" in cmd  # 네트워크는 허용
        assert cmd[-1] == "-"  # 프롬프트는 stdin으로 전달
        # 프롬프트는 argv가 아니라 stdin(input)으로 전달된다
        stdin = mock_run.call_args[1]["input"]
        assert "PREAMBLE" in stdin
        assert "UI를 구현하세요" in stdin

    def test_saves_output_json(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch.object(executor, "_run_tree", return_value=mock_result):
            executor._invoke_codex(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text(encoding="utf-8"))
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_codex(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_is_1800(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch.object(executor, "_run_tree", return_value=mock_result) as mock_run:
            executor._invoke_codex(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == 1800


# ---------------------------------------------------------------------------
# _run_verification (검증 게이트)
# ---------------------------------------------------------------------------

class TestRunVerification:
    def _write_settings(self, tmp_project, command):
        claude_dir = tmp_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": command}]}]}
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def test_no_settings_fails_closed(self, executor, tmp_project):
        # .claude/settings.json 없음 → 기본은 fail-closed(검증 실패로 간주)
        with patch.object(ex, "ROOT", tmp_project):
            ok, out = executor._run_verification()
        assert ok is False
        assert "fail-closed" in out

    def test_no_settings_passes_with_opt_out(self, executor, tmp_project):
        # --allow-no-verification 이면 설정이 없어도 통과(opt-out)
        executor._allow_no_verification = True
        with patch.object(ex, "ROOT", tmp_project):
            ok, out = executor._run_verification()
        assert ok is True
        assert out == ""

    def test_preflight_exits_when_no_settings(self, executor, tmp_project):
        # 설정 부재 시 preflight 가 fail-closed 로 즉시 중단한다.
        with patch.object(ex, "ROOT", tmp_project):
            with pytest.raises(SystemExit) as exc_info:
                executor._preflight_verification()
        assert exc_info.value.code == 1

    def test_preflight_passes_with_opt_out(self, executor, tmp_project):
        executor._allow_no_verification = True
        with patch.object(ex, "ROOT", tmp_project):
            executor._preflight_verification()  # 예외 없이 통과

    def test_passing_command(self, executor, tmp_project):
        self._write_settings(tmp_project, "exit 0")
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.object(ex, "ROOT", tmp_project), \
             patch.object(executor, "_run_tree", return_value=mock_result):
            ok, out = executor._run_verification()
        assert ok is True

    def test_failing_command(self, executor, tmp_project):
        self._write_settings(tmp_project, "exit 1")
        mock_result = MagicMock(returncode=1, stdout="build broke", stderr="err")
        with patch.object(ex, "ROOT", tmp_project), \
             patch.object(executor, "_run_tree", return_value=mock_result):
            ok, out = executor._run_verification()
        assert ok is False
        assert "build broke" in out

    def test_failure_detail_includes_command_and_exit(self, executor, tmp_project):
        # F19: 어떤 command 가 어떤 exit code 로 실패했는지 재시도 피드백에 담겨야 한다.
        self._write_settings(tmp_project, "npm run build")
        mock_result = MagicMock(returncode=2, stdout="TS error", stderr="")
        with patch.object(ex, "ROOT", tmp_project), \
             patch.object(executor, "_run_tree", return_value=mock_result):
            ok, out = executor._run_verification()
        assert ok is False
        assert "npm run build" in out   # 어떤 명령이
        assert "exit 2" in out          # 어떤 exit code 로
        assert "TS error" in out        # 출력 tail

    def test_completed_step_blocked_by_failing_verification(self, executor):
        """AI가 완료로 표기해도 검증 실패면 재시도 경로로 전환된다."""
        # AI 세션은 status를 completed로 바꾸고, invoke는 no-op으로 mock
        def mark_completed(step, preamble, **kwargs):
            idx = executor._read_json(executor._index_file)
            for s in idx["steps"]:
                if s["step"] == 2:
                    s["status"] = "completed"
            executor._write_json(executor._index_file, idx)
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "final_message": "HARNESS_STATUS: completed\nHARNESS_SUMMARY: ui done",
                    "codex": {"thread_id": None, "malformed": 0,
                              "turn_failed": None, "stream_error": None, "turn_completed": True}}

        with patch.object(executor, "_invoke_codex", side_effect=mark_completed), \
             patch.object(executor, "_run_verification", return_value=(False, "lint failed")), \
             patch.object(executor, "_commit_step") as mock_commit, \
             patch.object(executor, "_update_top_index"):
            with pytest.raises(SystemExit) as exc_info:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")

        # 3회 재시도 후에도 검증 실패 → error로 종료(exit 1), 성공 커밋은 없어야 함
        assert exc_info.value.code == 1
        idx = executor._read_json(executor._index_file)
        step2 = next(s for s in idx["steps"] if s["step"] == 2)
        assert step2["status"] == "error"
        assert "lint failed" in step2["error_message"]

    def test_error_updates_top_index_before_commit(self, executor):
        # 원자적 동기화: error 종료 시 top index 갱신이 커밋보다 먼저 일어나야 한다.
        order = []

        def noop_invoke(step, preamble, **kwargs):
            # status 를 갱신하지 않음 → "did not update" → 재시도 후 error
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "codex": {"thread_id": None, "malformed": 0,
                              "turn_failed": None, "stream_error": None, "turn_completed": True}}

        with patch.object(executor, "_invoke_codex", side_effect=noop_invoke), \
             patch.object(executor, "_run_verification", return_value=(True, "")), \
             patch.object(executor, "_update_top_index",
                          side_effect=lambda s: order.append(f"top:{s}")), \
             patch.object(executor, "_commit_step",
                          side_effect=lambda *a: order.append("commit")):
            with pytest.raises(SystemExit) as exc_info:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")

        assert exc_info.value.code == 1
        assert order == ["top:error", "commit"]

    def test_blocked_commits_and_orders_top_index_first(self, executor):
        # blocked 종료 시에도 두 index 를 커밋하고, top index 갱신이 커밋보다 먼저여야 한다.
        order = []

        def mark_blocked(step, preamble, **kwargs):
            idx = executor._read_json(executor._index_file)
            for s in idx["steps"]:
                if s["step"] == 2:
                    s["status"] = "blocked"
                    s["blocked_reason"] = "API key 필요"
            executor._write_json(executor._index_file, idx)
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "final_message": "HARNESS_STATUS: blocked\nHARNESS_REASON: API key 필요",
                    "codex": {"thread_id": None, "malformed": 0,
                              "turn_failed": None, "stream_error": None, "turn_completed": True}}

        with patch.object(executor, "_invoke_codex", side_effect=mark_blocked), \
             patch.object(executor, "_update_top_index",
                          side_effect=lambda s: order.append(f"top:{s}")), \
             patch.object(executor, "_commit_step",
                          side_effect=lambda *a: order.append("commit")):
            with pytest.raises(SystemExit) as exc_info:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")

        assert exc_info.value.code == 2
        assert order == ["top:blocked", "commit"]


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_context_manager(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self):
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index), encoding="utf-8")

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2

    def test_error_between_completed_steps_is_caught(self, tmp_project):
        # 비정상 배열 [completed, error, completed]: 역순+break 였다면 error 를 놓쳤다.
        steps = [
            {"step": 0, "name": "a", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
            {"step": 2, "name": "c", "status": "completed"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_finalize_guard_refuses_error_without_pending(self, tmp_project):
        # pending 이 없어도 error 가 남아 있으면 완료로 마킹하지 않고 exit 1.
        steps = [
            {"step": 0, "name": "a", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
            {"step": 2, "name": "c", "status": "completed"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        index = inst._read_json(inst._index_file)
        with patch.object(inst, "_update_top_index") as mock_top:
            with pytest.raises(SystemExit) as exc_info:
                inst._assert_all_completed(index)
        assert exc_info.value.code == 1
        mock_top.assert_called_once_with("error")

    def test_finalize_guard_passes_when_all_completed(self, tmp_project):
        steps = [
            {"step": 0, "name": "a", "status": "completed"},
            {"step": 1, "name": "b", "status": "completed"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        index = inst._read_json(inst._index_file)
        inst._assert_all_completed(index)  # 예외 없이 통과


# ---------------------------------------------------------------------------
# F5: codex preflight & 인프라 오류 분류
# ---------------------------------------------------------------------------

class TestPreflightCodex:
    def test_missing_codex_exits(self, executor):
        with patch("shutil.which", return_value=None):
            with pytest.raises(SystemExit) as exc:
                executor._preflight_codex()
        assert exc.value.code == 1

    def test_version_nonzero_exits(self, executor):
        with patch("shutil.which", return_value="/usr/bin/codex"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stdout="", stderr="not logged in")):
            with pytest.raises(SystemExit) as exc:
                executor._preflight_codex()
        assert exc.value.code == 1

    def test_version_launch_error_exits(self, executor):
        with patch("shutil.which", return_value="/usr/bin/codex"), \
             patch("subprocess.run", side_effect=FileNotFoundError("gone")):
            with pytest.raises(SystemExit) as exc:
                executor._preflight_codex()
        assert exc.value.code == 1

    def test_version_ok_passes(self, executor):
        with patch("shutil.which", return_value="/usr/bin/codex"), \
             patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="codex 1.0", stderr="")):
            executor._preflight_codex()  # 예외 없이 통과


class TestInvokeCodexInfra:
    def test_not_installed_records_infra_error(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch.object(executor, "_run_tree", side_effect=FileNotFoundError("codex not found")):
            output = executor._invoke_codex(step, "preamble")
        infra = output["infrastructure_error"]
        assert infra["kind"] == "not_found"
        assert infra["retryable"] is False
        assert output["exitCode"] is None
        # 프로세스 미생성이어도 output JSON 은 실패 근거로 보존된다
        out_file = executor._phase_dir / "step2-output.json"
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["infrastructure_error"]["kind"] == "not_found"

    def test_timeout_records_retryable_infra(self, executor):
        step = {"step": 2, "name": "ui"}
        exc = subprocess.TimeoutExpired(cmd="codex", timeout=1800)
        with patch.object(executor, "_run_tree", side_effect=exc):
            output = executor._invoke_codex(step, "preamble")
        infra = output["infrastructure_error"]
        assert infra["kind"] == "timeout"
        assert infra["retryable"] is True

    def test_launch_oserror_is_non_retryable(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch.object(executor, "_run_tree", side_effect=OSError("no resources")):
            output = executor._invoke_codex(step, "preamble")
        assert output["infrastructure_error"]["kind"] == "launch_error"
        assert output["infrastructure_error"]["retryable"] is False

    def test_success_has_no_infra_error(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch.object(executor, "_run_tree",
                          return_value=MagicMock(returncode=0, stdout="{}", stderr="")):
            output = executor._invoke_codex(step, "preamble")
        assert "infrastructure_error" not in output


class TestInfraHandlingInStep:
    def _infra(self, kind, retryable):
        return {"step": 2, "name": "ui", "exitCode": None,
                "infrastructure_error": {"kind": kind, "retryable": retryable, "detail": f"{kind} detail"}}

    def test_non_retryable_infra_fails_immediately(self, executor):
        calls = {"n": 0}
        def fake_invoke(step, preamble, **kwargs):
            calls["n"] += 1
            return self._infra("not_found", False)
        with patch.object(executor, "_invoke_codex", side_effect=fake_invoke), \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step"):
            with pytest.raises(SystemExit) as exc:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert exc.value.code == 1
        assert calls["n"] == 1  # 재시도하지 않고 즉시 중단
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert step2["status"] == "error"
        assert "not_found" in step2["error_message"]

    def test_non_retryable_infra_updates_top_index_before_commit(self, executor):
        order = []
        with patch.object(executor, "_invoke_codex", return_value=self._infra("not_found", False)), \
             patch.object(executor, "_update_top_index",
                          side_effect=lambda s: order.append(f"top:{s}")), \
             patch.object(executor, "_commit_step",
                          side_effect=lambda *a: order.append("commit")):
            with pytest.raises(SystemExit):
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert order == ["top:error", "commit"]

    def test_retryable_infra_retries_then_errors(self, executor):
        calls = {"n": 0}
        def fake_invoke(step, preamble, **kwargs):
            calls["n"] += 1
            return self._infra("timeout", True)
        with patch.object(executor, "_invoke_codex", side_effect=fake_invoke), \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step"):
            with pytest.raises(SystemExit) as exc:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert exc.value.code == 1
        assert calls["n"] == ex.StepExecutor.MAX_RETRIES  # 재시도 후 실패
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert step2["status"] == "error"
        assert "timeout" in step2["error_message"]


# ---------------------------------------------------------------------------
# F8: 검증 timeout & budget
# ---------------------------------------------------------------------------

class TestVerificationTimeout:
    def _write_settings(self, tmp_project, command):
        claude_dir = tmp_project / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": command}]}]}}
        (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    def test_command_timeout_returns_failure(self, executor, tmp_project):
        self._write_settings(tmp_project, "sleep 9999")
        with patch.object(ex, "ROOT", tmp_project), \
             patch.object(executor, "_run_tree",
                          side_effect=subprocess.TimeoutExpired(cmd="sleep", timeout=600)):
            ok, out = executor._run_verification()
        assert ok is False
        assert "timeout" in out

    def test_passes_timeout_kwarg_to_subprocess(self, executor, tmp_project):
        self._write_settings(tmp_project, "exit 0")
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch.object(ex, "ROOT", tmp_project), \
             patch.object(executor, "_run_tree", mock_run):
            executor._run_verification()
        assert "timeout" in mock_run.call_args[1]
        assert mock_run.call_args[1]["timeout"] > 0

    def test_no_settings_fails_closed(self, executor, tmp_project):
        # 설정이 아예 없으면 조용히 통과하지 않고 fail-closed 로 실패한다.
        with patch.object(ex, "ROOT", tmp_project):
            ok, out = executor._run_verification()
        assert ok is False
        assert "fail-closed" in out


# ---------------------------------------------------------------------------
# F6: codex JSONL 이벤트 파싱
# ---------------------------------------------------------------------------

class TestParseCodexEvents:
    HAPPY = "\n".join([
        '{"type":"thread.started","thread_id":"abc-123"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"first"}}',
        '{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"DONE"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":5}}',
    ])

    def test_extracts_thread_id(self, executor):
        assert executor._parse_codex_events(self.HAPPY)["thread_id"] == "abc-123"

    def test_final_message_is_last_agent_message(self, executor):
        assert executor._parse_codex_events(self.HAPPY)["final_message"] == "DONE"

    def test_extracts_usage_and_turn_completed(self, executor):
        r = executor._parse_codex_events(self.HAPPY)
        assert r["turn_completed"] is True
        assert r["usage"]["output_tokens"] == 5

    def test_malformed_line_counted(self, executor):
        r = executor._parse_codex_events(self.HAPPY + "\n{ this is not json")
        assert r["malformed"] == 1

    def test_turn_failed_captured(self, executor):
        r = executor._parse_codex_events('{"type":"turn.failed","error":"model overloaded"}')
        assert r["turn_failed"] == "model overloaded"

    def test_stream_error_captured(self, executor):
        r = executor._parse_codex_events('{"type":"error","message":"auth expired"}')
        assert r["stream_error"] == "auth expired"

    def test_failed_command_captured(self, executor):
        stream = ('{"type":"item.completed","item":{"id":"i","type":"command_execution",'
                  '"command":"npm test","aggregated_output":"boom","exit_code":1,"status":"completed"}}')
        r = executor._parse_codex_events(stream)
        assert len(r["failed_commands"]) == 1
        assert r["failed_commands"][0]["exit_code"] == 1
        assert r["failed_commands"][0]["command"] == "npm test"

    def test_successful_command_not_flagged(self, executor):
        stream = ('{"type":"item.completed","item":{"id":"i","type":"command_execution",'
                  '"command":"ls","aggregated_output":"","exit_code":0,"status":"completed"}}')
        assert executor._parse_codex_events(stream)["failed_commands"] == []

    def test_in_progress_command_null_exit_not_flagged(self, executor):
        stream = ('{"type":"item.started","item":{"id":"i","type":"command_execution",'
                  '"command":"npm test","aggregated_output":"","exit_code":null,"status":"in_progress"}}')
        assert executor._parse_codex_events(stream)["failed_commands"] == []

    def test_empty_stream(self, executor):
        r = executor._parse_codex_events("")
        assert r["thread_id"] is None
        assert r["malformed"] == 0
        assert r["turn_completed"] is False


class TestTail:
    def test_short_unchanged(self, executor):
        assert executor._tail("abc", 10) == "abc"

    def test_keeps_tail_drops_head(self, executor):
        out = executor._tail("HEAD" + "x" * 100 + "TAILERR", 20)
        assert out.endswith("TAILERR")
        assert "HEAD" not in out

    def test_none_safe(self, executor):
        assert executor._tail(None, 10) == ""


class TestCodexRunError:
    def _clean(self, **over):
        # 성공 계약을 만족하는 기본 result(exit 0 + turn_completed + 오류 없음).
        codex = {"malformed": 0, "turn_failed": None, "stream_error": None, "turn_completed": True}
        codex.update(over.pop("codex", {}))
        result = {"exitCode": 0, "codex": codex}
        result.update(over)
        return result

    def test_none_when_clean(self, executor):
        assert executor._codex_run_error(self._clean()) is None

    def test_malformed_flagged(self, executor):
        assert "malformed" in executor._codex_run_error(self._clean(codex={"malformed": 2}))

    def test_turn_failed_flagged(self, executor):
        assert "boom" in executor._codex_run_error(self._clean(codex={"turn_failed": "boom"}))

    def test_stream_error_flagged(self, executor):
        assert "authX" in executor._codex_run_error(self._clean(codex={"stream_error": "authX"}))

    def test_nonzero_exit_flagged(self, executor):
        # F16: codex 가 completed 를 써도 exit code 가 0 이 아니면 성공으로 인정하지 않는다.
        msg = executor._codex_run_error(self._clean(exitCode=1))
        assert "exit 1" in msg

    def test_missing_turn_completed_flagged(self, executor):
        # F6b: turn.completed 이벤트가 없으면 스트림이 잘렸다고 보고 실패 처리.
        msg = executor._codex_run_error(self._clean(codex={"turn_completed": False}))
        assert "turn.completed" in msg

    def test_empty_result_flagged(self, executor):
        # codex 요약이 아예 없으면(정상 실행이라면 항상 있어야 함) fail-closed 로 실패 처리.
        assert executor._codex_run_error({}) is not None


class TestInvokeCodexParsingAndResume:
    HAPPY = "\n".join([
        '{"type":"thread.started","thread_id":"sess-9"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"DONE"}}',
        '{"type":"turn.completed","usage":{"output_tokens":7}}',
    ])

    def test_output_has_parsed_fields(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch.object(executor, "_run_tree",
                          return_value=MagicMock(returncode=0, stdout=self.HAPPY, stderr="")):
            out = executor._invoke_codex(step, "preamble")
        assert out["thread_id"] == "sess-9"
        assert out["final_message"] == "DONE"
        assert out["usage"]["output_tokens"] == 7
        assert out["codex"]["thread_id"] == "sess-9"

    def test_preserves_attempt_jsonl_per_attempt(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch.object(executor, "_run_tree",
                          return_value=MagicMock(returncode=0, stdout=self.HAPPY, stderr="")):
            executor._invoke_codex(step, "preamble", attempt=2)
        raw = executor._phase_dir / "step2-attempt-2.jsonl"
        assert raw.exists()
        assert "thread.started" in raw.read_text(encoding="utf-8")

    def test_fresh_call_uses_exec_with_step_file(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch.object(executor, "_run_tree",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")) as m:
            executor._invoke_codex(step, "PREAMBLE\n")
        cmd = m.call_args[0][0]
        assert cmd[:2] == ["codex", "exec"]
        assert "resume" not in cmd
        stdin = m.call_args[1]["input"]
        assert "PREAMBLE" in stdin
        assert "UI를 구현하세요" in stdin  # 새 세션은 step 파일을 재주입

    def test_resume_call_uses_resume_subcommand(self, executor):
        step = {"step": 2, "name": "ui"}
        with patch.object(executor, "_run_tree",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")) as m:
            executor._invoke_codex(step, "FEEDBACK", attempt=2, resume_thread_id="sess-9")
        cmd = m.call_args[0][0]
        assert cmd[:3] == ["codex", "exec", "resume"]
        assert "sess-9" in cmd
        # resume 은 -s/--sandbox 를 못 받으므로 샌드박스는 -c sandbox_mode 로 지정한다.
        assert "-s" not in cmd
        assert "sandbox_mode=workspace-write" in cmd
        stdin = m.call_args[1]["input"]
        assert stdin == "FEEDBACK"          # resume 은 피드백만
        assert "UI를 구현하세요" not in stdin  # step 파일 재주입 안 함


class TestResumeIntegration:
    def test_retry_resumes_captured_thread(self, executor):
        calls = []
        def fake_invoke(step, preamble, attempt=1, resume_thread_id=None):
            calls.append(resume_thread_id)
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "codex": {"thread_id": "T-1", "malformed": 0}}
        with patch.object(executor, "_invoke_codex", side_effect=fake_invoke), \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step"):
            with pytest.raises(SystemExit):
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert calls[0] is None       # 첫 시도는 fresh
        assert calls[1] == "T-1"      # 재시도는 resume
        assert calls[2] == "T-1"

    def test_no_thread_id_falls_back_to_stateless(self, executor):
        calls = []
        def fake_invoke(step, preamble, attempt=1, resume_thread_id=None):
            calls.append(resume_thread_id)
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "codex": {"thread_id": None, "malformed": 0}}
        with patch.object(executor, "_invoke_codex", side_effect=fake_invoke), \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step"):
            with pytest.raises(SystemExit):
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert all(c is None for c in calls)  # thread_id 없으면 계속 stateless

    def test_completed_but_malformed_not_accepted(self, executor):
        def mark_completed_malformed(step, preamble, attempt=1, resume_thread_id=None):
            idx = executor._read_json(executor._index_file)
            for s in idx["steps"]:
                if s["step"] == 2:
                    s["status"] = "completed"
            executor._write_json(executor._index_file, idx)
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "final_message": "HARNESS_STATUS: completed",
                    "codex": {"thread_id": "T", "malformed": 3}}
        with patch.object(executor, "_invoke_codex", side_effect=mark_completed_malformed), \
             patch.object(executor, "_run_verification", return_value=(True, "")) as mock_verify, \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step"):
            with pytest.raises(SystemExit) as exc:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert exc.value.code == 1
        mock_verify.assert_not_called()  # malformed 면 검증 이전에 재시도
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert step2["status"] == "error"
        assert "malformed" in step2["error_message"]

    def _mark_completed_returning(self, executor, result):
        def _fn(step, preamble, attempt=1, resume_thread_id=None):
            idx = executor._read_json(executor._index_file)
            for s in idx["steps"]:
                if s["step"] == 2:
                    s["status"] = "completed"
            executor._write_json(executor._index_file, idx)
            result.setdefault("final_message",
                              "HARNESS_STATUS: completed\nHARNESS_SUMMARY: done")  # F10c: verdict 보고
            return result
        return _fn

    def test_completed_but_nonzero_exit_not_accepted(self, executor):
        # F16: codex 가 index 를 completed 로 써도 exitCode != 0 이면 성공으로 인정하지 않는다.
        result = {"step": 2, "name": "ui", "exitCode": 1,
                  "codex": {"thread_id": "T", "malformed": 0, "turn_failed": None,
                            "stream_error": None, "turn_completed": True}}
        with patch.object(executor, "_invoke_codex",
                          side_effect=self._mark_completed_returning(executor, result)), \
             patch.object(executor, "_run_verification", return_value=(True, "")) as mock_verify, \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step"):
            with pytest.raises(SystemExit) as exc:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert exc.value.code == 1
        mock_verify.assert_not_called()  # exit!=0 → 검증 이전에 실패
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert step2["status"] == "error"
        assert "exit 1" in step2["error_message"]

    def test_completed_but_no_turn_completed_not_accepted(self, executor):
        # F6b: turn.completed 이벤트가 없으면 completed 여도 인정하지 않는다.
        result = {"step": 2, "name": "ui", "exitCode": 0,
                  "codex": {"thread_id": "T", "malformed": 0, "turn_failed": None,
                            "stream_error": None, "turn_completed": False}}
        with patch.object(executor, "_invoke_codex",
                          side_effect=self._mark_completed_returning(executor, result)), \
             patch.object(executor, "_run_verification", return_value=(True, "")) as mock_verify, \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step"):
            with pytest.raises(SystemExit) as exc:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert exc.value.code == 1
        mock_verify.assert_not_called()
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert "turn.completed" in step2["error_message"]

    def test_completed_clean_run_is_accepted(self, executor):
        # 대비: exit 0 + turn_completed + 오류 없음 이면 정상적으로 검증→수락된다.
        result = {"step": 2, "name": "ui", "exitCode": 0,
                  "codex": {"thread_id": "T", "malformed": 0, "turn_failed": None,
                            "stream_error": None, "turn_completed": True}}
        with patch.object(executor, "_invoke_codex",
                          side_effect=self._mark_completed_returning(executor, result)), \
             patch.object(executor, "_run_verification", return_value=(True, "")) as mock_verify, \
             patch.object(executor, "_update_top_index"), \
             patch.object(executor, "_commit_step") as mock_commit:
            ok = executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert ok is True
        mock_verify.assert_called()   # 성공 계약 만족 → 검증 게이트 진입
        mock_commit.assert_called_once()


class TestParseVerdict:
    def test_completed_with_summary(self, executor):
        v = executor._parse_verdict("작업함\nHARNESS_STATUS: completed\nHARNESS_SUMMARY: did the thing")
        assert v["status"] == "completed"
        assert v["summary"] == "did the thing"

    def test_blocked_with_reason(self, executor):
        v = executor._parse_verdict("HARNESS_STATUS: blocked\nHARNESS_REASON: need API key")
        assert v["status"] == "blocked"
        assert v["reason"] == "need API key"

    def test_error_status(self, executor):
        v = executor._parse_verdict("HARNESS_STATUS: error\nHARNESS_REASON: 복구 불가")
        assert v["status"] == "error"
        assert v["reason"] == "복구 불가"

    def test_no_report_is_none(self, executor):
        assert executor._parse_verdict("그냥 작업만 함")["status"] is None

    def test_none_message_safe(self, executor):
        assert executor._parse_verdict(None)["status"] is None

    def test_case_insensitive_and_unknown_ignored(self, executor):
        assert executor._parse_verdict(
            "harness_status: COMPLETED\nHARNESS_SUMMARY: 완료")["status"] == "completed"
        assert executor._parse_verdict("HARNESS_STATUS: weird")["status"] is None

    # --- 엄격한 계약 강제 ---

    def test_completed_without_summary_rejected(self, executor):
        # completed 는 비어있지 않은 summary 를 요구한다(없으면 거부 → 재시도).
        assert executor._parse_verdict("HARNESS_STATUS: completed")["status"] is None

    def test_blocked_without_reason_rejected(self, executor):
        assert executor._parse_verdict("HARNESS_STATUS: blocked")["status"] is None

    def test_duplicate_status_rejected(self, executor):
        # HARNESS_STATUS 줄이 둘 이상이면(예시 인용 등) 모호하므로 거부한다.
        msg = ("HARNESS_STATUS: completed\nHARNESS_SUMMARY: did it\n"
               "HARNESS_STATUS: completed")
        assert executor._parse_verdict(msg)["status"] is None


class TestVerdictDrivenStatus:
    def test_harness_records_completed_from_verdict_not_index(self, executor):
        # F10c: codex 가 index.json 을 건드리지 않아도, HARNESS_STATUS 보고만으로 harness 가 상태를 기록한다.
        def report_only(step, preamble, **kwargs):
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "final_message": "HARNESS_STATUS: completed\nHARNESS_SUMMARY: built ui",
                    "codex": {"thread_id": None, "malformed": 0, "turn_failed": None,
                              "stream_error": None, "turn_completed": True}}
        with patch.object(executor, "_invoke_codex", side_effect=report_only), \
             patch.object(executor, "_run_verification", return_value=(True, "")), \
             patch.object(executor, "_commit_step"), \
             patch.object(executor, "_update_top_index"):
            ok = executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert ok is True
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert step2["status"] == "completed"
        assert step2["summary"] == "built ui"

    def test_no_verdict_retries_then_errors(self, executor):
        # HARNESS_STATUS 미보고(codex 가 index 도 안 씀) → 재시도 후 error 로 기록.
        def report_nothing(step, preamble, **kwargs):
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "final_message": "작업은 했지만 상태 보고를 빠뜨림",
                    "codex": {"thread_id": None, "malformed": 0, "turn_failed": None,
                              "stream_error": None, "turn_completed": True}}
        with patch.object(executor, "_invoke_codex", side_effect=report_nothing), \
             patch.object(executor, "_commit_step"), \
             patch.object(executor, "_update_top_index"):
            with pytest.raises(SystemExit) as exc:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert exc.value.code == 1
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert step2["status"] == "error"
        assert "HARNESS_STATUS" in step2["error_message"]

    def test_blocked_verdict_from_failed_run_not_accepted(self, executor):
        # R4: 프로토콜 오류(malformed)가 있으면 잘린 부분 메시지의 HARNESS_STATUS: blocked 를 믿지 않는다.
        def report_blocked_but_malformed(step, preamble, **kwargs):
            return {"step": 2, "name": "ui", "exitCode": 0,
                    "final_message": "HARNESS_STATUS: blocked\nHARNESS_REASON: need key",
                    "codex": {"thread_id": None, "malformed": 3, "turn_failed": None,
                              "stream_error": None, "turn_completed": True}}
        with patch.object(executor, "_invoke_codex", side_effect=report_blocked_but_malformed), \
             patch.object(executor, "_commit_step"), \
             patch.object(executor, "_update_top_index"):
            with pytest.raises(SystemExit) as exc:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        # blocked(exit 2)로 확정되지 않고 재시도 후 error(exit 1)로 끝나야 한다.
        assert exc.value.code == 1
        step2 = next(s for s in executor._read_json(executor._index_file)["steps"] if s["step"] == 2)
        assert step2["status"] == "error"


class TestFeedbackBuilders:
    def test_verify_feedback_keeps_tail(self, executor):
        out = "HEADMARK" + "x" * 5000 + "REALERROR"
        fb = executor._build_verify_feedback(out, None)
        assert "REALERROR" in fb
        assert "HEADMARK" not in fb  # 앞부분은 잘리고 tail 보존

    def test_verify_feedback_includes_failed_commands(self, executor):
        codex = {"failed_commands": [{"command": "npm test", "exit_code": 1, "output": "assert fail"}]}
        fb = executor._build_verify_feedback("short", codex)
        assert "npm test" in fb
        assert "assert fail" in fb

    def test_resume_prompt_has_feedback_and_contract(self, executor):
        p = executor._build_resume_prompt("SOME FEEDBACK")
        assert "SOME FEEDBACK" in p
        assert "index.json" in p
        assert "커밋" in p  # 커밋 금지 계약 유지


# ---------------------------------------------------------------------------
# 원자적 쓰기 · repo 락 (F10)
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_no_temp_file_left_behind(self, tmp_path):
        p = tmp_path / "a.json"
        ex.StepExecutor._write_json(p, {"k": "v"})
        # temp→os.replace 후 임시(.tmp) 파일이 디렉터리에 남지 않아야 한다.
        leftovers = [f.name for f in tmp_path.iterdir() if f.name != "a.json"]
        assert leftovers == []

    def test_overwrite_shorter_leaves_no_trailing_bytes(self, tmp_path):
        p = tmp_path / "a.json"
        ex.StepExecutor._write_json(p, {"big": "x" * 2000})
        ex.StepExecutor._write_json(p, {"s": 1})
        # in-place truncate 가 아니라 원자 교체이므로 이전 긴 내용이 꼬리로 남지 않는다.
        assert ex.StepExecutor._read_json(p) == {"s": 1}

    def test_roundtrip_content_matches(self, tmp_path):
        p = tmp_path / "a.json"
        data = {"steps": [{"step": 0, "n": "한글"}], "extra": 3}
        ex.StepExecutor._write_json(p, data)
        assert ex.StepExecutor._read_json(p) == data


class TestRepoLock:
    def test_second_acquire_is_blocked(self, tmp_path):
        lockfile = tmp_path / ".harness.lock"
        a = ex.RepoLock(lockfile)
        b = ex.RepoLock(lockfile)
        assert a.acquire() is True
        try:
            # 이미 잡혀 있으면 비차단으로 False 를 반환한다(호출자는 fail-fast).
            assert b.acquire() is False
        finally:
            a.release()
        # 해제 후에는 다시 획득 가능하다(OS advisory lock, stale 없음).
        assert b.acquire() is True
        b.release()

    def test_release_without_acquire_is_noop(self, tmp_path):
        ex.RepoLock(tmp_path / ".harness.lock").release()  # 예외 없이 통과


# ---------------------------------------------------------------------------
# 프로세스 트리 종료 (F5b)
# ---------------------------------------------------------------------------

class TestRunTree:
    def test_returns_completed_process(self, executor):
        # 정상 종료 시 CompletedProcess 로 stdout/returncode 를 담아 반환한다.
        r = executor._run_tree([sys.executable, "-c", "print('hi')"], timeout=30)
        assert r.returncode == 0
        assert "hi" in r.stdout

    def test_passes_stdin_input(self, executor):
        r = executor._run_tree([sys.executable, "-c",
                                "import sys;sys.stdout.write(sys.stdin.read().upper())"],
                               input="abc", timeout=30)
        assert "ABC" in r.stdout

    def test_timeout_raises_and_kills_tree(self, executor):
        # timeout 시 TimeoutExpired 를 올리고, 트리 종료(_kill_tree)가 호출돼야 한다(F5b).
        with patch.object(executor, "_kill_tree", wraps=executor._kill_tree) as spy:
            with pytest.raises(subprocess.TimeoutExpired):
                executor._run_tree([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        spy.assert_called_once()

    def test_kill_tree_handles_dead_process(self, executor):
        # 이미 죽은 프로세스에 _kill_tree 를 호출해도 예외 없이 통과한다.
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        ex.StepExecutor._kill_tree(p)  # 예외 없이 통과
