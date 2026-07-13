#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push]
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# Windows 콘솔(cp949)에서도 한글·유니코드 기호(—, 스피너 등)를 출력할 수 있도록 utf-8 강제.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


# --- 무결성: 원자적 쓰기 · 동시 실행 락 (F10) ---


def _atomic_write_text(path: Path, text: str):
    """임시 파일에 쓴 뒤 os.replace 로 원자 교체한다.

    쓰기 도중 크래시가 나도 대상 파일은 항상 '이전 완전본' 또는 '새 완전본'만 보인다
    (부분 기록/손상 방지). 임시 파일을 같은 디렉터리에 만들어 os.replace 가 같은 파일시스템
    안에서 원자적으로 동작하도록 보장한다(Windows·POSIX 모두 원자적 교체).
    (전원 장애 durability 는 목표가 아니다 — 필요해지면 디렉터리 fsync 를 별도로 추가한다.)
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class RepoLock:
    """repo 단위 동시 실행 락. 두 harness 실행이 같은 repo 의 git·index.json 을 동시에
    건드려 상태가 깨지는 것을 막는다.

    OS advisory lock(Windows msvcrt / POSIX fcntl)을 쓰므로 프로세스가 죽으면 커널이 자동
    해제한다(stale lock 파일이 남아도 무의미). 비차단(non-blocking): 이미 잡혀 있으면
    acquire() 가 False 를 반환하고, 호출자는 fail-fast 로 중단한다.
    """

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self) -> bool:
        fh = open(self._path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self):
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    FEAT_MSG = "feat({phase}): step {num} — {name}"
    CHORE_MSG = "chore({phase}): step {num} output"
    TZ = timezone(timedelta(hours=9))
    CODEX_TIMEOUT = 1800          # codex step 실행 timeout (초)
    VERIFY_CMD_TIMEOUT = 600      # 검증 커맨드 1개당 timeout (초)

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False,
                 allow_no_verification: bool = False):
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push
        self._allow_no_verification = allow_no_verification
        # 검증 명령 스냅샷: preflight 에서 Stop 훅 명령 목록을 한 번 읽어 고정한다(codex 가 실행 중
        # settings.json 을 바꿔 게이트를 무력화하거나 임의 명령을 심는 것을 차단). (commands, error) 튜플.
        self._verify_snapshot = None

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])

    def run(self):
        # repo 단위 동시 실행 락(F10): 두 harness 가 같은 repo 의 git·index.json 을 동시에
        # 건드려 상태가 깨지는 것을 막는다. 비차단 — 이미 잡혀 있으면 fail-fast.
        lock = RepoLock(self._phases_dir / ".harness.lock")
        if not lock.acquire():
            print("\n  ERROR: 다른 harness 실행이 이미 이 repo 에서 진행 중입니다 "
                  f"(락: {self._phases_dir / '.harness.lock'}).")
            print("  동시에 두 실행이 git·index.json 을 건드리면 상태가 깨지므로 중단합니다. "
                  "다른 실행이 끝난 뒤 다시 시도하세요.")
            sys.exit(1)
        try:
            self._print_header()
            self._preflight_codex()
            self._preflight_verification()
            self._check_blockers()
            self._preflight_clean_worktree()
            self._checkout_branch()
            guardrails = self._load_guardrails()
            self._ensure_created_at()
            self._execute_all_steps(guardrails)
            self._finalize()
        finally:
            lock.release()

    # --- timestamps ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        # 원자적 쓰기(temp→os.replace)로 쓰기 도중 크래시에도 파일이 손상되지 않게 한다(F10).
        _atomic_write_text(p, json.dumps(data, indent=2, ensure_ascii=False))

    def _snapshot_state_files(self) -> dict:
        """codex 실행 직전 상태 파일(phase index·top index)의 원본을 캡처한다(F10c-r).

        codex 는 workspace-write 라 지시를 어기고 phases/**/index.json 을 쓸 수 있다. 실행 직후
        이 스냅샷으로 되돌리면(_restore_state_files) codex 의 index 변경이 무효화되고, 이후 harness 가
        verdict 로만 상태를 기록한다. 부재 파일은 None 으로 기록해 복원 시 codex 가 새로 만든 것을 지운다.
        """
        snap = {}
        for p in (self._index_file, self._top_index_file):
            try:
                snap[p] = p.read_text(encoding="utf-8")
            except OSError:
                snap[p] = None
        return snap

    def _restore_state_files(self, snap: dict):
        """스냅샷과 현재 내용이 다르면 상태 파일을 원본으로 되돌린다(codex 변경 무효화)."""
        for p, original in snap.items():
            try:
                current = p.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == original:
                continue
            if original is None:
                with contextlib.suppress(OSError):
                    p.unlink()  # codex 가 없던 파일을 만든 경우 제거
            else:
                _atomic_write_text(p, original)

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def _git_or_fail(self, args, what: str) -> subprocess.CompletedProcess:
        """git mutation 을 실행하고 실패하면 fail-closed 로 중단한다(F10b).

        add/reset 같은 스테이징 조작이 조용히 실패하면 커밋 경계가 깨지므로(메타데이터가 feat 에
        섞이는 등), 반환코드를 검사해 실패 시 상태 기록 실패로 간주하고 harness 를 중단한다.
        (git diff --cached --quiet 처럼 반환코드로 신호를 주는 조회성 호출에는 쓰지 않는다.)
        """
        r = self._run_git(*args)
        if r.returncode != 0:
            self._fail_commit(what, r.stderr)
        return r

    def _head(self) -> Optional[str]:
        r = self._run_git("rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else None

    # --- subprocess (프로세스 트리 종료) ---

    def _run_tree(self, cmd, *, input=None, timeout, shell=False) -> subprocess.CompletedProcess:
        """subprocess.run 과 유사하되, timeout 시 자식 프로세스 트리 전체를 종료한다(F5b).

        subprocess.run(timeout=)은 직접 자식만 kill 하므로 bash/npm/test 가 띄운 손자
        프로세스(dev 서버·watcher·test worker)가 살아남아 포트·worktree 를 오염시킨다.
        POSIX 는 새 세션(process group)으로 실행 후 그룹에 TERM→KILL, Windows 는 taskkill /T 로
        트리를 종료한다. timeout 시 subprocess.TimeoutExpired 를 그대로 올려 호출자 계약을 유지한다.
        """
        popen_kwargs = dict(
            cwd=self._root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input is not None else None,
            text=True, encoding="utf-8", errors="replace", shell=shell,
        )
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True  # 새 세션 → 자식이 프로세스 그룹 리더

        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            # 트리를 죽인 뒤 파이프를 드레인한다(손자까지 죽어야 EOF 가 나 블로킹이 풀린다).
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen):
        """proc 을 루트로 하는 프로세스 트리 전체를 종료한다(플랫폼별)."""
        if os.name == "nt":
            # taskkill /T 로 자식 트리까지 강제 종료. 실패해도 아래 proc.kill 로 최소 직접 자식은 종료.
            with contextlib.suppress(OSError):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            with contextlib.suppress(OSError):
                proc.kill()
            return
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            with contextlib.suppress(OSError):
                proc.kill()
            return
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)  # TERM 에 대한 graceful 유예
        except subprocess.TimeoutExpired:
            pass
        # 직접 자식이 먼저 죽었어도 TERM 을 무시한 손자가 그룹에 남을 수 있으므로, 유예 후 그룹
        # 전체에 SIGKILL 을 보낸다(이미 비었으면 ProcessLookupError → 무시). 이후 자식을 reap 한다.
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            proc.wait(timeout=5)

    def _preflight_clean_worktree(self):
        """실행 전, phases/ 를 제외한 워크스페이스가 clean 한지 강제한다.

        _commit_step 은 git add -A 로 스테이징하므로, 시작 시점에 무관한 미커밋 변경이 있으면
        그것이 feat 커밋에 섞인다. phases/ 는 harness 메타데이터라 허용하고, 그 외 dirty 가
        하나라도 있으면 중단시켜 커밋 무결성을 보장한다.
        """
        r = self._run_git("status", "--porcelain")
        if r.returncode != 0:
            return  # git 사용 불가 문제는 _checkout_branch 에서 별도 처리
        dirty = []
        for ln in r.stdout.splitlines():
            path = ln[3:].strip()  # porcelain: 'XY <path>'
            if path and not path.startswith("phases/"):
                dirty.append(ln)
        if dirty:
            print("\n  ERROR: 커밋되지 않은 변경이 있습니다 (phases/ 제외).")
            for ln in dirty[:10]:
                print(f"    {ln}")
            if len(dirty) > 10:
                print(f"    ... 외 {len(dirty) - 10}개")
            print("  feat 커밋에 무관한 변경이 섞이지 않도록 먼저 stash 또는 commit 하세요.")
            sys.exit(1)

    def _normalize_codex_commits(self, pre_head: Optional[str]):
        """codex 가 규칙을 어기고 직접 커밋한 경우, soft-reset 으로 harness 커밋으로 되돌린다.

        커밋은 harness(_commit_step)가 전담해야 feat/chore 분리가 유지된다. codex 가 만든
        커밋을 pre_head 까지 soft-reset 하면 변경은 워크트리/인덱스에 그대로 남고, 이어서
        harness 가 규칙대로 다시 커밋한다. 실패한 시도에서 만든 커밋도 매번 정규화된다.
        """
        if not pre_head:
            return
        cur = self._head()
        if cur and cur != pre_head:
            r = self._run_git("reset", "--soft", pre_head)
            if r.returncode == 0:
                print("  ⚠ codex 가 직접 커밋함 → soft-reset 으로 harness 커밋으로 정규화")
            else:
                print(f"  WARN: codex 커밋 정규화 실패: {r.stderr.strip()}")

    def _checkout_branch(self):
        branch = f"feat-{self._phase_name}"

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print(f"  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print(f"  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _commit_step(self, step_num: int, step_name: str):
        # 불변식: _preflight_clean_worktree() 로 시작 시점 워크트리가 clean(phases/ 제외)하고,
        # 각 step 이 자기 산출물을 커밋하므로, 여기서 잡히는 코드 변경은 이번 step 에서 codex 가
        # 만든 것뿐이다(무관한 사용자 변경이 섞이지 않는다).
        #
        # 상태 소유권(F10c-r): phases/ 는 harness 전용 상태 영역이다. codex 가 지시를 어기고
        # phases/** 아래에 다른 phase 의 index 나 새 파일을 남겨도 git 이력에 들어가면 안 된다.
        # 따라서 feat 는 phases 를 전부 unstage 해 코드만 담고, chore 는 harness 가 소유하는 두
        # index 파일만 명시 pathspec 으로 stage 한다(과거 `git add -A` 스윕이 stray phases 파일을
        # feat 에 섞던 구멍을 구조적으로 차단 — F10c-r 보강).
        # (stepN-output.json·attempt jsonl 은 .gitignore 대상이라 커밋 대상이 아니다 — 명시 add 시
        #  ignored 경로 에러가 나므로 포함하지 않는다.)
        index_rel = f"phases/{self._phase_dir_name}/index.json"
        top_index_rel = "phases/index.json"

        # feat: 코드(비 phases)만 커밋. phases/ 는 전부 unstage 해 어떤 상태 파일도 섞이지 않게 한다.
        # git mutation 은 반환코드를 검사해 실패 시 fail-closed 로 중단한다(F10b — 커밋 경계 보장).
        self._git_or_fail(["add", "-A"], "코드 스테이징(git add) 실패")
        self._git_or_fail(["reset", "HEAD", "--", "phases"], "phases unstage(git reset) 실패")

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.FEAT_MSG.format(phase=self._phase_name, num=step_num, name=step_name)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                self._fail_commit("코드(feat) 커밋 실패", r.stderr)
            print(f"  Commit: {msg}")

        # chore: harness 소유 상태(phase index + top index)만 명시 pathspec 으로 stage 해 단일
        # 커밋으로 함께 기록한다. 호출자는 _commit_step 전에 두 index 를 모두 갱신해 둔다.
        self._git_or_fail(["add", "--", index_rel, top_index_rel],
                          "메타데이터 스테이징(git add) 실패")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(phase=self._phase_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                self._fail_commit("메타데이터(chore) 커밋 실패", r.stderr)

    def _fail_commit(self, what: str, stderr: str):
        # 커밋 실패는 상태를 git 이력에 기록하지 못한다는 뜻이므로 warning 이 아닌 harness 실패로 처리한다.
        print(f"\n  ERROR: {what} — 상태를 기록할 수 없어 중단합니다.")
        if stderr:
            print(f"  {stderr.strip()}")
        sys.exit(1)

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- guardrails & context ---

    def _load_guardrails(self) -> str:
        sections = []
        claude_md = ROOT / "CLAUDE.md"
        if claude_md.exists():
            sections.append(f"## 프로젝트 규칙 (CLAUDE.md)\n\n{claude_md.read_text(encoding='utf-8')}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                sections.append(f"## {doc.stem}\n\n{doc.read_text(encoding='utf-8')}")
        text = "\n\n---\n\n".join(sections) if sections else ""
        self._preflight_templates(text)
        return text

    @staticmethod
    def _preflight_templates(guardrails: str):
        """CLAUDE.md·docs 에 미작성 템플릿 플레이스홀더(`{한글…}`)가 남아 있으면 중단한다(fail-closed).

        가드레일은 매 step 프롬프트에 그대로 주입되므로, 안 채운 `{프로젝트명}`·`{프레임워크}` 같은
        리터럴이 codex 에게 진짜 규칙처럼 전달돼 작업을 오도한다. 베이스를 복사해 시작할 때 가장 흔한 실수.
        (한글로 시작하는 중괄호만 매칭 — 코드/JSON 의 `{...}` 오탐 회피.)
        """
        placeholders = list(dict.fromkeys(re.findall(r"\{[가-힣][^{}]*\}", guardrails)))
        if not placeholders:
            return
        print("\n  ERROR: CLAUDE.md/docs 에 미작성 템플릿 플레이스홀더가 남아 있습니다:")
        for ph in placeholders[:8]:
            print(f"    {ph}")
        if len(placeholders) > 8:
            print(f"    ... 외 {len(placeholders) - 8}개")
        print("  이 값이 매 step 프롬프트에 그대로 주입돼 codex 를 오도합니다. 먼저 채우세요(예: /project-init).")
        sys.exit(1)

    @staticmethod
    def _build_step_context(index: dict) -> str:
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    def _build_preamble(self, guardrails: str, step_context: str,
                        prev_error: Optional[str] = None) -> str:
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
                f"{prev_error}\n\n---\n\n"
            )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{retry_section}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"3. 기존 테스트를 깨뜨리지 마라.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라.\n"
            f"5. index.json 을 비롯한 어떤 harness 상태 파일(phases/**)도 직접 수정하지 마라.\n"
            f"   대신 이 작업의 결과를 응답의 맨 마지막에 아래 형식으로 '정확히 한 번'만 보고하라\n"
            f"   (harness 가 이 보고를 읽어 상태를 기록한다. 본문에서 이 형식을 예시로 인용하지 마라 —\n"
            f"    HARNESS_STATUS 가 둘 이상이면 모호하므로 거부되고 재시도된다):\n"
            f"   - AC 통과 → (HARNESS_SUMMARY 필수)\n"
            f"       HARNESS_STATUS: completed\n"
            f"       HARNESS_SUMMARY: <이 step 산출물을 한 줄로 요약>\n"
            f"   - 사용자 개입 필요(API 키·인증·수동 설정 등) → (HARNESS_REASON 필수)\n"
            f"       HARNESS_STATUS: blocked\n"
            f"       HARNESS_REASON: <필요한 개입>\n"
            f"   - 고칠 수 없는 실패 → (HARNESS_REASON 필수)\n"
            f"       HARNESS_STATUS: error\n"
            f"       HARNESS_REASON: <실패 원인>\n"
            f"6. 절대 git 커밋·푸시를 하지 마라. 커밋은 harness(execute.py)가 feat/chore 로 분리해 전담한다.\n"
            f"   너는 코드 변경까지만 수행하고, index.json 수정·git add/commit/push 는 실행하지 마라.\n\n---\n\n"
        )

    # --- codex 호출 ---

    def _preflight_codex(self):
        """codex CLI 가 설치·실행 가능한지 시작 시 확인한다 (fail-fast).

        미설치/실행 불가 상태로 step 루프에 진입하면 매 step·재시도가 무의미하게 반복되므로,
        시작 시 한 번 검증해 명확한 오류로 즉시 중단한다. (인증 오류는 codex 가 실행은 되지만
        비정상 종료하므로 _invoke_codex 의 infrastructure_error 분류로 잡는다.)
        """
        if shutil.which("codex") is None:
            print("\n  ERROR: codex CLI 를 찾을 수 없습니다 (PATH 에 없음).")
            print("  codex 를 설치하고 로그인(codex login)한 뒤 다시 시도하세요.")
            sys.exit(1)
        try:
            r = subprocess.run(
                ["codex", "--version"], cwd=self._root,
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"\n  ERROR: codex 실행 확인에 실패했습니다: {e}")
            sys.exit(1)
        if r.returncode != 0:
            print(f"\n  ERROR: `codex --version` 이 비정상 종료했습니다 (code {r.returncode}).")
            if r.stderr:
                print(f"  {r.stderr.strip()}")
            print("  codex 설치·로그인 상태를 확인하세요.")
            sys.exit(1)

    @staticmethod
    def _as_text(v) -> str:
        """subprocess 출력(str|bytes|None)을 안전하게 문자열로 정규화한다.

        TimeoutExpired.stdout/stderr 는 CPython 버전에 따라 bytes 로 올 수 있어 방어한다.
        """
        if v is None:
            return ""
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        return v

    @staticmethod
    def _tail(text: str, limit: int) -> str:
        """문자열의 마지막 limit 글자만 남긴다(오류는 출력 뒤쪽에 있는 경우가 많다)."""
        text = text or ""
        if len(text) <= limit:
            return text
        return "…(앞부분 생략)…\n" + text[-limit:]

    def _parse_codex_events(self, stdout: str) -> dict:
        """codex `--json` JSONL 이벤트 스트림을 파싱해 구조화 결과를 추출한다.

        기존에는 스트림을 문자열로만 저장해 성공/실패를 codex 가 고친 index 에만 의존했다.
        이제 이벤트를 직접 읽어 최종 응답·오류·토큰 사용량·세션 id 를 뽑고, JSON 파싱이
        깨진 라인(malformed)을 집계해 결과를 계약대로 판정할 수 있게 한다.
        """
        summary = {
            "thread_id": None,        # resume 용 세션 id (thread.started)
            "final_message": None,    # 마지막 agent_message 텍스트
            "usage": None,            # turn.completed 의 토큰 사용량
            "turn_completed": False,  # turn.completed 이벤트 관측 여부
            "turn_failed": None,      # turn.failed 오류
            "stream_error": None,     # 최상위 error 이벤트
            "malformed": 0,           # JSON 파싱 실패 라인 수
            "failed_commands": [],    # 비정상 종료한 command_execution
        }
        for ln in (stdout or "").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                summary["malformed"] += 1
                continue
            if not isinstance(ev, dict):
                summary["malformed"] += 1
                continue
            etype = ev.get("type")
            if etype == "thread.started":
                summary["thread_id"] = ev.get("thread_id")
            elif etype == "turn.completed":
                summary["turn_completed"] = True
                summary["usage"] = ev.get("usage")
            elif etype == "turn.failed":
                err = ev.get("error") or ev.get("message")
                summary["turn_failed"] = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
            elif etype == "error":
                msg = ev.get("message") or ev.get("error")
                summary["stream_error"] = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
            elif etype == "item.completed":
                item = ev.get("item") or {}
                itype = item.get("type")
                if itype == "agent_message":
                    summary["final_message"] = item.get("text")
                elif itype == "command_execution":
                    code = item.get("exit_code")
                    if isinstance(code, int) and code != 0:
                        summary["failed_commands"].append({
                            "command": item.get("command"),
                            "exit_code": code,
                            "output": self._tail(item.get("aggregated_output"), 800),
                        })
        return summary

    def _invoke_codex(self, step: dict, preamble: str, attempt: int = 1,
                      resume_thread_id: Optional[str] = None) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        # codex exec = 비대화형(headless) 모드. 프롬프트는 argv 길이 한계를 피하려 stdin('-')으로 전달.
        # -s workspace-write: 모든 쓰기를 워크스페이스로 가둬 repo 밖 시스템을 보호 (안전축 복원).
        #   network_access=true 로 네트워크는 허용 → step 중 npm install 등 정상 동작.
        #   워크스페이스 밖 escalation은 exec 비대화형에서 자동 거부(fail-safe).
        #   빌드 중 워크스페이스 밖 접근이 꼭 필요한 프로젝트라면 이 두 인자를
        #   ["--dangerously-bypass-approvals-and-sandbox"] 로 바꾸면 전권 실행된다.
        if resume_thread_id:
            # 재시도(F7): 같은 세션을 resume 해 codex 가 이전 맥락(가드레일·step·앞선 시도)을 유지하게 한다.
            # stateless 새 세션 + 잘린 문자열 피드백 대신, 구조화된 실패 피드백만 이어서 보낸다.
            # resume 서브커맨드는 -s/--sandbox 를 받지 않으므로 샌드박스를 -c sandbox_mode 로 지정한다.
            cmd = ["codex", "exec", "resume", resume_thread_id,
                   "-c", "sandbox_mode=workspace-write",
                   "-c", "sandbox_workspace_write.network_access=true",
                   "--json", "-"]
            prompt = preamble  # resume 프롬프트(가드레일·step 재주입 없음 — 이미 세션 맥락에 있음)
        else:
            cmd = ["codex", "exec",
                   "-s", "workspace-write",
                   "-c", "sandbox_workspace_write.network_access=true",
                   "--json", "-"]
            prompt = preamble + step_file.read_text(encoding="utf-8")

        infra_error = None
        try:
            result = self._run_tree(cmd, input=prompt, timeout=self.CODEX_TIMEOUT)
            returncode, stdout, stderr = result.returncode, result.stdout, result.stderr
        except FileNotFoundError as e:
            # codex 실행 파일이 사라짐/미설치 — 재시도해도 동일하게 실패하므로 재시도 불가로 분류.
            returncode, stdout, stderr = None, "", str(e)
            infra_error = {"kind": "not_found", "retryable": False,
                           "detail": "codex 실행 파일을 찾을 수 없음"}
        except subprocess.TimeoutExpired as e:
            # timeout 시 _run_tree 가 codex 프로세스 트리 전체를 종료한다(F5b). 일시적일 수 있어 재시도 가능.
            returncode = None
            stdout, stderr = self._as_text(e.stdout), self._as_text(e.stderr)
            infra_error = {"kind": "timeout", "retryable": True,
                           "detail": f"codex 가 {self.CODEX_TIMEOUT}s 내에 완료되지 않음"}
        except OSError as e:
            # 프로세스 생성 자체 실패(권한/리소스 등) — 재시도 불가로 분류.
            returncode, stdout, stderr = None, "", str(e)
            infra_error = {"kind": "launch_error", "retryable": False, "detail": str(e)}

        # 원본 JSONL 스트림을 attempt 별로 보존한다(덮어쓰지 않음). .gitignore 로 커밋은 제외.
        if stdout:
            raw_path = self._phase_dir / f"step{step_num}-attempt-{attempt}.jsonl"
            raw_path.write_text(stdout, encoding="utf-8")

        codex = self._parse_codex_events(stdout)

        if infra_error:
            print(f"\n  WARN: codex 실행 실패 [{infra_error['kind']}] — {infra_error['detail']}")
        elif returncode != 0:
            print(f"\n  WARN: codex가 비정상 종료됨 (code {returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
        elif codex["turn_failed"] or codex["stream_error"] or codex["malformed"]:
            print(f"\n  WARN: codex 실행 결과 이상 "
                  f"(turn_failed={bool(codex['turn_failed'])}, "
                  f"stream_error={bool(codex['stream_error'])}, malformed={codex['malformed']})")

        # output JSON 은 파싱된 구조화 요약만 저장한다(원본 스트림은 attempt 파일에 별도 보존).
        output = {
            "step": step_num, "name": step_name, "attempt": attempt,
            "exitCode": returncode,
            "thread_id": codex["thread_id"],
            "final_message": codex["final_message"],
            "usage": codex["usage"],
            "turn_completed": codex["turn_completed"],
            "turn_failed": codex["turn_failed"],
            "stream_error": codex["stream_error"],
            "malformed": codex["malformed"],
            "failed_commands": codex["failed_commands"],
            "stderr_tail": self._tail(stderr, 2000),
        }
        if infra_error:
            output["infrastructure_error"] = infra_error
        # subprocess 미생성(미설치/timeout)이어도 output JSON 을 남겨 실패 근거를 보존한다.
        out_path = self._phase_dir / f"step{step_num}-output.json"
        self._write_json(out_path, output)  # 원자적 쓰기(F10)

        output["codex"] = codex  # 호출자 편의를 위해 파싱 요약도 함께 반환(파일에는 미저장)
        return output

    # --- 검증 게이트 ---

    def _load_verification_commands(self) -> tuple:
        """.claude/settings.json 의 Stop 훅 검증 커맨드 목록을 로드한다.

        반환: (commands, error). error 가 None 이 아니면 설정이 부재/손상/비어있음을 뜻한다.
        """
        settings = ROOT / ".claude" / "settings.json"
        if not settings.exists():
            return [], ".claude/settings.json 없음"
        try:
            cfg = self._read_json(settings)
        except (json.JSONDecodeError, OSError) as e:
            return [], f".claude/settings.json 파싱 실패: {e}"
        commands = [
            h["command"]
            for group in cfg.get("hooks", {}).get("Stop", [])
            for h in group.get("hooks", [])
            if h.get("type") == "command" and h.get("command")
        ]
        if not commands:
            return [], "Stop 훅에 실행할 검증 명령이 없음"
        return commands, None

    def _preflight_verification(self):
        """실행 전 검증 설정이 유효한지 확인한다 (fail-closed).

        설정이 부재/손상/비어있으면 잘못된 working tree가 조용히 통과하는 것을 막기 위해
        즉시 중단한다. 검증 없이 진행하려면 --allow-no-verification 를 명시해야 한다.

        여기서 Stop 훅 명령 목록을 한 번 읽어 스냅샷으로 고정한다(codex 실행 이전). 이후
        _run_verification 은 이 스냅샷을 재사용하므로, codex 가 실행 중 settings.json 을 바꿔
        게이트를 무력화하거나 임의 명령을 심는 것을 차단한다.
        """
        # codex 가 아직 돌기 전에 스냅샷을 잡는다(allow-no-verification 이어도 고정해 둔다).
        self._verify_snapshot = self._load_verification_commands()
        if self._allow_no_verification:
            print("  WARN: 검증 게이트 비활성화됨 (--allow-no-verification)")
            return
        _, err = self._verify_snapshot
        if err:
            print(f"\n  ERROR: 검증 설정이 유효하지 않습니다 — {err}")
            print(f"  기본값은 fail-closed 입니다. 검증 없이 진행하려면 --allow-no-verification 를 명시하세요.")
            sys.exit(1)

    def _run_verification(self) -> tuple:
        """검증 커맨드(lint/build/test)를 실행한다.

        codex 세션은 Claude Code 훅을 발동시키지 않으므로, execute.py가 대신 게이트를 건다.
        --allow-no-verification 이면 게이트를 건너뛴다(opt-out). 그 외에 설정이 부재/손상/비어
        있으면 fail-closed 로 실패시킨다(검증 무결성 보장).

        각 커맨드에 command_timeout(VERIFY_CMD_TIMEOUT) 초 한도를 걸어 무기한 hang 을 차단한다.
        반환: (통과 여부, 실패 시 출력).
        """
        if self._allow_no_verification:
            return True, ""
        snap = getattr(self, "_verify_snapshot", None)
        commands, err = snap if snap is not None else self._load_verification_commands()
        if err:
            # preflight 에서 이미 걸러졌어야 하지만, 방어적으로 실패 처리한다.
            return False, f"검증 설정 오류(fail-closed): {err}"
        # Stop 훅 명령은 POSIX sh 문법([ -f ], &&, 2>&1)으로 작성되며 Claude Code도 sh로 실행한다.
        # Windows의 cmd.exe는 이 문법을 못 파싱하므로, bash가 있으면 bash로 실행해 동작을 일치시킨다.
        # 명령 출처는 사용자 입력이 아니라 레포 자체 설정(신뢰됨)이다.
        bash = shutil.which("bash")
        for cmd in commands:
            try:
                if bash:
                    r = self._run_tree([bash, "-c", cmd], timeout=self.VERIFY_CMD_TIMEOUT)
                else:
                    r = self._run_tree(cmd, timeout=self.VERIFY_CMD_TIMEOUT, shell=True)
            except subprocess.TimeoutExpired:
                # timeout 시 _run_tree 가 검증 명령 프로세스 트리 전체를 종료한다(F5b). 재시도 경로로 넘긴다.
                return False, f"검증 명령 timeout ({self.VERIFY_CMD_TIMEOUT}s 초과): {cmd}"
            if r.returncode != 0:
                # F19: 어떤 명령이 어떤 exit code 로 실패했는지 헤더에 담아 재시도가 헛돌지 않게 한다.
                # 출력은 tail 로 잘라 총량을 제한하되 헤더(명령·exit)는 항상 보존한다.
                out = self._tail((r.stdout + r.stderr).strip(), 1500)
                return False, f"명령 `{cmd}` 실패 (exit {r.returncode})\n{out}"
        return True, ""

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total}")
        if self._auto_push:
            print(f"  Auto-push: enabled")
        print(f"{'='*60}")

    def _check_blockers(self):
        # 전체 step 을 정방향으로 스캔한다. 역순+break 로 하면 error/blocked 뒤에 completed 가
        # 오는 비정상 배열([completed, error, completed])에서 error 를 놓쳐, pending 이 없을 때
        # phase 가 완료로 잘못 마킹된다. 위치와 무관하게 첫 error/blocked 에서 중단한다.
        index = self._read_json(self._index_file)
        for s in index["steps"]:
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print(f"  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print(f"  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 실행 루프 ---

    def _fail_infrastructure(self, step: dict, infra: dict, elapsed: int):
        """재시도가 무의미한 인프라 오류(codex 미설치/프로세스 생성 실패 등)를 즉시 실패로 기록한다.

        최종 실패 경로와 동일하게 error 상태를 두 index 에 기록하고 단일 chore 커밋으로 남긴 뒤
        중단한다(워크트리와 git 이력의 원자적 동기화 유지).
        """
        step_num, step_name = step["step"], step["name"]
        ts = self._stamp()
        index = self._read_json(self._index_file)
        for s in index["steps"]:
            if s["step"] == step_num:
                s["status"] = "error"
                s["error_message"] = f"[인프라 오류: {infra['kind']}] {infra['detail']}"
                s["failed_at"] = ts
        self._write_json(self._index_file, index)
        self._update_top_index("error")
        self._commit_step(step_num, step_name)
        print(f"\n  ✗ Step {step_num}: {step_name} — 인프라 오류로 중단 [{elapsed}s]")
        print(f"    {infra['kind']}: {infra['detail']}")
        sys.exit(1)

    def _build_resume_prompt(self, feedback: str) -> str:
        """codex 세션 resume 시 보낼 프롬프트(F7).

        가드레일·step 지시는 이미 세션 맥락에 있으므로 재주입하지 않고, 구조화된 실패 피드백과
        완료 계약만 이어서 전달한다(stateless 새 세션 + 잘린 문자열 피드백을 대체).
        """
        return (
            "이전 시도가 실패했습니다. 이 세션에는 이미 프로젝트 규칙·step 지시·앞선 변경 맥락이 있습니다.\n"
            "아래 실패 정보를 바탕으로 원인을 고치세요.\n\n"
            f"{feedback}\n\n---\n\n"
            "고친 뒤 반드시 응답 맨 마지막에 결과를 '정확히 한 번'만 보고하라(index.json 등 상태 파일은\n"
            "직접 수정하지 말고, 본문에서 이 형식을 예시로 인용하지 마라 — 중복되면 거부되고 재시도된다):\n"
            "- AC 통과 시: (HARNESS_SUMMARY 필수)\n"
            "    HARNESS_STATUS: completed\n"
            "    HARNESS_SUMMARY: <산출물 한 줄 요약>\n"
            "- 사용자 개입 필요(API 키·인증·수동 설정 등): (HARNESS_REASON 필수)\n"
            "    HARNESS_STATUS: blocked\n"
            "    HARNESS_REASON: <필요한 개입>\n"
            "- 실패가 계속되면: (HARNESS_REASON 필수)\n"
            "    HARNESS_STATUS: error\n"
            "    HARNESS_REASON: <실패 원인>\n"
            "- 절대 git 커밋/푸시를 하지 마라(커밋은 harness 가 전담).\n"
        )

    def _build_verify_feedback(self, verify_out: str, codex: Optional[dict]) -> str:
        """검증 실패를 구조화해 재시도 피드백으로 만든다(F7).

        오류는 출력 뒤쪽에 몰리므로 tail 을 보존하고(앞부분만 자르던 문제 수정), codex 가 실행 중
        비정상 종료시킨 명령도 exit code·출력과 함께 붙인다.
        """
        parts = ["## 검증 실패 (lint/build/test)",
                 "검증 출력(마지막 부분):\n" + self._tail(verify_out, 2000)]
        failed = (codex or {}).get("failed_commands") or []
        if failed:
            lines = ["codex 실행 중 실패한 명령:"]
            for c in failed[:5]:
                lines.append(f"- `{c['command']}` (exit {c['exit_code']})\n{c['output']}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def _codex_run_error(self, result: dict) -> Optional[str]:
        """codex 실행이 성공 계약을 만족하는지 판정해, 못 하면 사유를 반환한다(F6·F16).

        성공 조건을 한 곳에 모은다: exitCode==0 AND turn.completed 관측 AND 프로토콜 오류 없음
        (malformed/turn.failed/error). 하나라도 어기면 codex 가 index 를 completed 로 바꿔놨더라도
        '완료'로 인정하지 않고 재시도 경로로 보낸다(fail-closed — 결과를 index 에만 의존하지 않음).
        인프라 오류(프로세스 미생성)는 호출자가 먼저 걸러내므로 여기서 exitCode is None 은 판정 제외.
        """
        codex = result.get("codex") or {}
        problems = []
        exit_code = result.get("exitCode")
        if exit_code not in (0, None):
            problems.append(f"codex 비정상 종료 (exit {exit_code})")
        if codex.get("malformed"):
            problems.append(
                f"codex JSONL 이벤트 {codex['malformed']}줄이 손상됨(malformed) — 스트림이 잘렸을 수 있음")
        if codex.get("turn_failed"):
            problems.append(f"codex turn 실패: {codex['turn_failed']}")
        if codex.get("stream_error"):
            problems.append(f"codex 오류 이벤트: {codex['stream_error']}")
        if not codex.get("turn_completed"):
            problems.append("codex turn.completed 이벤트가 없음 — 실행이 끝까지 완료되지 않았거나 스트림이 잘림")
        return "; ".join(problems) if problems else None

    @staticmethod
    def _parse_verdict(final_message: Optional[str]) -> dict:
        """codex 최종 메시지의 HARNESS_STATUS 보고를 엄격히 파싱한다(F10c).

        codex 는 index.json 을 직접 쓰지 말고 결과를 응답 맨 마지막에 이 형식으로 보고하도록
        프롬프트로 지시받으며, harness 가 이를 읽어 상태를 기록한다(상태의 논리적 소유권은 harness).
        계약을 엄격히 강제한다: HARNESS_STATUS 는 정확히 하나여야 하고(본문에서 예시·과거 결과를
        인용해 중복되면 모호하므로 거부), completed 는 비어있지 않은 summary, blocked/error 는
        비어있지 않은 reason 을 요구한다. 계약을 못 지키면 status=None 으로 두어 재시도 경로로 흐른다.
        """
        verdict = {"status": None, "summary": None, "reason": None}
        statuses, summary, reason = [], None, None
        for ln in (final_message or "").splitlines():
            s = ln.strip()
            low = s.lower()
            if low.startswith("harness_status:"):
                statuses.append(s.split(":", 1)[1].strip().lower())
            elif low.startswith("harness_summary:"):
                summary = s.split(":", 1)[1].strip() or None
            elif low.startswith("harness_reason:"):
                reason = s.split(":", 1)[1].strip() or None
        # 정확히 하나의 유효 status 만 인정한다. 부재·중복(예시 인용 포함)·미지값은 거부 → None.
        valid = [v for v in statuses if v in ("completed", "blocked", "error")]
        if len(statuses) != 1 or len(valid) != 1:
            return verdict
        status = valid[0]
        # 필수 필드 계약: completed→summary, blocked/error→reason. 누락 시 거부(재시도).
        if status == "completed" and not summary:
            return verdict
        if status in ("blocked", "error") and not reason:
            return verdict
        verdict.update(status=status, summary=summary, reason=reason)
        return verdict

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """단일 step 실행 (재시도 포함). 완료되면 True, 실패/차단이면 False."""
        step_num, step_name = step["step"], step["name"]
        done = sum(1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed")
        prev_error = None
        thread_id = None  # 첫 시도에서 codex 세션 id 를 확보해 재시도 때 resume 한다(F7).

        for attempt in range(1, self.MAX_RETRIES + 1):
            # 재시도 & 세션 id 확보 시: 같은 codex 세션을 resume 해 맥락을 유지하고 피드백만 이어 보낸다.
            # 그 외(첫 시도/세션 id 미확보): 가드레일+step 전체를 담은 새 세션 프롬프트를 만든다.
            resume_id = thread_id if (attempt > 1 and thread_id) else None
            if resume_id:
                prompt = self._build_resume_prompt(prev_error or "")
            else:
                index = self._read_json(self._index_file)
                step_context = self._build_step_context(index)
                prompt = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            pre_head = self._head()
            state_snap = self._snapshot_state_files()  # codex 가 index 를 건드려도 되돌리기 위해 캡처(F10c-r)
            with progress_indicator(tag) as pi:
                result = self._invoke_codex(step, prompt, attempt=attempt, resume_thread_id=resume_id)
            # pi.elapsed 는 progress_indicator 의 finally 에서 세팅되므로 with 블록을 벗어난 뒤 읽는다.
            elapsed = int(pi.elapsed)
            # codex 가 규칙을 어기고 직접 커밋했다면 harness 커밋으로 정규화한다.
            self._normalize_codex_commits(pre_head)
            # codex 가 상태 파일을 직접 수정했다면 스냅샷으로 되돌린다 — 상태 기록은 harness 전담(F10c-r).
            self._restore_state_files(state_snap)

            # 첫 시도에서 세션 id 를 확보해 이후 재시도에서 resume 에 사용한다.
            if thread_id is None:
                thread_id = (result.get("codex") or {}).get("thread_id")

            # codex 프로세스 자체가 실행되지 못한 인프라 오류(미설치/timeout/생성 실패) 분류.
            infra = result.get("infrastructure_error")
            if infra and not infra.get("retryable", False):
                # 미설치·프로세스 생성 실패 등 재시도가 무의미한 오류 → 즉시 실패 처리(중단).
                self._fail_infrastructure(step, infra, elapsed)

            index = self._read_json(self._index_file)
            # codex 보고(HARNESS_STATUS)에서 상태를 파생한다 — 상태의 논리적 소유권은 harness(F10c).
            # 미보고면 status=None → 재시도.
            verdict = self._parse_verdict(result.get("final_message"))
            status = verdict["status"]
            ts = self._stamp()

            # 재시도 가능한 인프라 오류(timeout 등): codex 가 status 를 못 바꿨으므로 아래 재시도 경로로
            # 흐르되, "did not update status" 대신 명확한 인프라 사유를 재시도 프롬프트에 전달한다.
            infra_err = None
            if infra:
                infra_err = f"[codex {infra['kind']}] {infra['detail']}"

            # codex 실행이 프로토콜 레벨에서 실패(malformed/turn.failed)면 index 가 completed 여도
            # 완료로 인정하지 않고 재시도한다(F6 — 결과를 index 에만 의존하지 않고 이벤트로 판정).
            codex_err = self._codex_run_error(result) if not infra else None
            # verdict(completed/blocked)는 '깨끗한 실행'에서만 신뢰한다. infra 오류·프로토콜 오류
            # (malformed/turn.failed/비정상 종료)가 있으면, 잘린 부분 메시지에 HARNESS_STATUS 가
            # 있더라도 완료·차단으로 확정하지 않고 재시도 경로로 보낸다(R4 리뷰 반영).
            run_ok = not infra and not codex_err

            verify_err = None
            if status == "completed" and run_ok:
                # codex 가 완료를 보고하면, 인정하기 전에 검증 게이트(lint/build/test)를 한 번 돌린다.
                ok, verify_out = self._run_verification()
                if ok:
                    for s in index["steps"]:
                        if s["step"] == step_num:
                            s["status"] = "completed"  # harness 가 상태를 기록(F10c)
                            if verdict["summary"]:
                                s["summary"] = verdict["summary"]
                            s["completed_at"] = ts
                    self._write_json(self._index_file, index)
                    self._commit_step(step_num, step_name)
                    print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                    return True
                # 검증(lint/build/test) 실패 → 완료로 인정하지 않고 재시도 경로로 전환
                verify_err = self._build_verify_feedback(verify_out, result.get("codex"))
                print(f"  ✗ Step {step_num}: 검증 실패 — 재시도 대상")

            if status == "blocked" and run_ok:
                reason = verdict["reason"] or ""
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "blocked"  # harness 가 상태를 기록(F10c)
                        s["blocked_reason"] = reason
                        s["blocked_at"] = ts
                self._write_json(self._index_file, index)
                # 두 index 를 모두 갱신한 뒤 커밋한다. 기존에는 blocked 상태가 아예 커밋되지 않아
                # 워크트리와 git 이력이 어긋났다(부분 산출물도 함께 커밋해 복구 가능하게 남긴다).
                self._update_top_index("blocked")
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {reason}")
                self._commit_step(step_num, step_name)
                sys.exit(2)

            # codex 가 error 로 보고했거나 아무 status 도 보고하지 않은 경우 → 재시도/에러.
            reported_err = verdict["reason"] if status == "error" else None
            err_msg = (verify_err or codex_err or infra_err or reported_err or
                       "codex 가 HARNESS_STATUS 를 보고하지 않음(completed/blocked/error 미보고)")

            if attempt < self.MAX_RETRIES:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                        s.pop("error_message", None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "error"
                        s["error_message"] = f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                        s["failed_at"] = ts
                self._write_json(self._index_file, index)
                # 두 index 를 모두 갱신한 뒤 단일 chore 커밋으로 함께 기록한다(원자적 동기화).
                self._update_top_index("error")
                self._commit_step(step_num, step_name)
                print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]")
                print(f"    Error: {err_msg}")
                sys.exit(1)

        return False  # unreachable

    def _assert_all_completed(self, index: dict):
        """pending 이 없을 때, 모든 step 이 completed 인지 검증한다(fail-closed).

        error/blocked/그 외 상태가 하나라도 남아 있으면 phase 를 완료로 마킹하지 않고 중단한다.
        _check_blockers 가 시작 시 걸러주지만, 실행 중 상태 변화에 대한 방어선으로 다시 확인한다.
        """
        for s in index["steps"]:
            st = s["status"]
            if st == "completed":
                continue
            if st == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) 가 blocked 상태 — phase 를 완료로 마킹하지 않습니다.")
                self._update_top_index("blocked")
                sys.exit(2)
            print(f"\n  ✗ Step {s['step']} ({s['name']}) status={st} — phase 를 완료로 마킹하지 않습니다.")
            self._update_top_index("error")
            sys.exit(1)

    def _execute_all_steps(self, guardrails: str):
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                self._assert_all_completed(index)
                print("\n  All steps completed!")
                return

            step_num = pending["step"]
            for s in index["steps"]:
                if s["step"] == step_num and "started_at" not in s:
                    s["started_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    break

            self._execute_single_step(pending, guardrails)

    def _finalize(self):
        index = self._read_json(self._index_file)
        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._phase_name}): mark phase completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                self._fail_commit("phase 완료 커밋 실패", r.stderr)
            print(f"  ✓ {msg}")

        if self._auto_push:
            branch = f"feat-{self._phase_name}"
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    parser.add_argument("--allow-no-verification", action="store_true",
                        help="검증 설정이 없어도 진행 (기본은 fail-closed로 중단)")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, auto_push=args.push,
                 allow_no_verification=args.allow_no_verification).run()


if __name__ == "__main__":
    main()
