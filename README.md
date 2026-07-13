# Harness Framework (codex 버전)

step 파일 기반 개발 하네스. 사람이 작업을 잘게 쪼갠 **step 파일**을 만들면, `scripts/execute.py`가 step마다 **독립된 codex 세션**을 띄워 순차 실행·자가 교정·자동 커밋한다.

> 이 저장소는 [jha0313/harness_framework](https://github.com/jha0313/harness_framework)를 fork해, AI 실행 백엔드를 **Claude CLI → codex CLI**로 전환한 버전이다.

---

## codex 전환 요약

| 항목 | 원본 (claude) | 이 저장소 (codex) |
|---|---|---|
| AI 호출 | `claude -p --dangerously-skip-permissions --output-format json <prompt>` | `codex exec -s workspace-write -c sandbox_workspace_write.network_access=true --json -` |
| 프롬프트 전달 | argv | **stdin** (argv 길이 한계 회피) |
| 실행 권한 | 전권 | **workspace-write 샌드박스** + 네트워크 허용 |
| 검증 (lint/build/test) | Claude Code `Stop` 훅 | **execute.py 검증 게이트** (훅이 codex에선 안 뜨므로 직접 실행) |
| 프로젝트 메모리 | `CLAUDE.md` | `CLAUDE.md`(주입) + `AGENTS.md`(codex 네이티브) |

### 왜 바꿨나 — 핵심 함정
Claude Code의 `.claude/settings.json` 훅(`Stop`=검증, `PreToolUse`=위험명령 차단)은 **codex 세션에서 발동하지 않는다**. 그래서 CLI만 바꾸면 검증·안전 축이 조용히 사라진다. 이를 execute.py 내부로 이전해 등가 동작을 유지했다.

---

## 6축 구조

| 축 | 구현 |
|---|---|
| **구조** | 클론 즉시 `scripts/`·`docs/`·`.claude/`·`CLAUDE.md` 골격 존재 |
| **맥락** | `CLAUDE.md` + `docs/*.md`를 `_load_guardrails()`가 매 프롬프트에 주입 |
| **계획** | `.claude/commands/harness.md`(`/harness`) 워크플로우로 `phases/<task>/step{N}.md` 생성 |
| **실행** | `execute.py`가 `codex exec`로 step 실행, 완료 summary를 다음 프롬프트에 누적 |
| **검증** | step 완료 후 검증 게이트(lint/build/test) 실행 — 통과해야 완료 인정 |
| **무결성** | repo 동시 실행 락 + index.json 원자적 쓰기(temp→os.replace) + 프로세스 트리 종료 (F10·F5b) |
| **안전·개선** | workspace-write 샌드박스 + feat 브랜치 격리 + error/blocked 복구 루프 |

---

## 사용법

### 1. step 설계
`/harness` 워크플로우(`.claude/commands/harness.md`)에 따라 작업을 여러 step으로 쪼갠다.

### 2. 파일 생성
```
phases/index.json                 # 전체 task 현황
phases/<task>/index.json          # task별 step 목록 + status
phases/<task>/step{N}.md          # step마다 1개 (작업 지시 + AC)
```

### 3. 실행
```bash
python scripts/execute.py <task>          # 순차 실행
python scripts/execute.py <task> --push   # 실행 후 브랜치 push
```

execute.py가 자동 처리하는 것:
- **codex preflight** (시작 시 codex 설치·실행 확인, 미설치면 즉시 명확한 오류로 중단)
- `feat-<task>` 브랜치 생성/checkout
- 가드레일 주입 (CLAUDE.md + docs)
- 컨텍스트 누적 (완료 step의 summary를 다음 프롬프트에 전달)
- **JSONL 이벤트 파싱 + 성공 판정** (codex `--json` 스트림에서 최종 응답·오류·토큰 사용량·세션 id 추출. 성공 조건은 `exitCode==0 · turn.completed 관측 · 프로토콜 오류(malformed/turn.failed) 없음`으로 fail-closed 판정. 원본은 `stepN-attempt-{n}.jsonl`로 보존)
- **상태 보고(HARNESS_STATUS) 파싱** (codex는 `index.json`을 직접 쓰지 않고 응답 **맨 마지막**에 `HARNESS_STATUS: completed|blocked|error`(+summary/reason)를 정확히 한 번 보고. harness가 이를 읽어 상태를 기록 → **상태 판정의 단일 소유자**. 미보고·중복·필수필드 누락 시 재시도 — F10c)
- 자가 교정 (실패 시 최대 3회 재시도. **같은 codex 세션을 resume**해 맥락 유지 + 실패 command·exit code·출력 tail을 구조화 피드백으로 전달)
- **인프라 오류 분류** (codex 미설치/생성 실패=재시도 불가로 즉시 중단, timeout=재시도)
- **검증 게이트** (lint/build/test 통과해야 step 완료 인정, command별 timeout. 실패 시 **어떤 명령이 어떤 exit code로** 실패했는지 재시도 피드백에 포함 — F19)
- **프로세스 트리 종료** (codex·검증 명령을 프로세스 그룹으로 실행, timeout 시 손자 프로세스까지 종료 — Windows `taskkill /T` / POSIX `killpg`. orphan npm/node의 포트·worktree 오염 차단 — F5b)
- 2단계 커밋 (코드 `feat` / 메타데이터 `chore` 분리)
- 타임스탬프 자동 기록
- **동시 실행 락 + 원자적 상태 쓰기** (repo 락으로 두 실행 동시 진입 차단, index.json은 temp→`os.replace`로 원자 교체 — 아래 [무결성](#무결성--동시-실행-락--원자적-쓰기-f10) 참고)

### 에러 복구
- **error**: `phases/<task>/index.json`에서 해당 step `status`를 `pending`으로, `error_message` 삭제 후 재실행
- **blocked**: `blocked_reason` 해결 후 `status`를 `pending`으로, `blocked_reason` 삭제 후 재실행

---

## 안전 (샌드박스)

codex는 `-s workspace-write` + 네트워크 허용으로 실행된다.
- ✅ 모든 쓰기가 repo 워크스페이스로 제한 → **repo 밖 시스템 보호**
- ✅ 네트워크 허용 → `npm install` 등 정상 동작
- 워크스페이스 밖 접근은 비대화형 exec에서 자동 거부 (fail-safe)

repo 내부 파괴(`rm -rf`, `reset --hard`)는 샌드박스로 못 막지만, **feat 브랜치 격리 + step마다 커밋**으로 복구 가능하다.

> 빌드 중 워크스페이스 밖 접근이 꼭 필요하면 `scripts/execute.py`의 codex argv를 `--dangerously-bypass-approvals-and-sandbox`(전권)로 바꾼다.

---

## 검증 게이트 & Stop 훅

검증 명령은 `.claude/settings.json`의 `Stop` 훅 하나로 관리한다. 두 곳에서 이 명령을 사용한다:
- **Claude Code 세션 종료 시** — Claude Code가 sh로 실행 (대화형 개발 중 검증)
- **codex step 완료 시** — `execute.py`의 검증 게이트가 **bash로 실행** (codex는 Claude Code 훅을 발동시키지 않으므로 execute.py가 대신 건다)

기본 명령:
```
npm run lint 2>&1 && npm run build 2>&1 && npm run test 2>&1
```

- **fail-closed 기본**: 검증 게이트는 조용히 통과하지 않는다. `package.json`조차 없는 상태(빈 템플릿 등)면 `npm run lint`가 실패해 step 이 완료로 인정되지 않는다. 아직 애플리케이션이 실행 가능한 단계가 아니면 `--allow-no-verification`으로 **명시적으로** opt-out 하거나, 그 단계 전용 검증 명령을 둔다. (예전의 `[ -f package.json ] || exit 0` 묵시적 no-op 가드는 명시적 opt-out과 중복이자 fail-open 이라 제거했다 — 조용한 성공보다 명시적 예외.)
- 명령은 POSIX sh 문법이므로 `execute.py`는 이를 **bash로 실행**한다 (Windows `cmd.exe`가 `&&`·redirection을 못 파싱하는 문제 회피, bash 없으면 shell fallback).
- 다른 스택이면 이 명령만 프로젝트에 맞게 바꾸면 된다 (예: `pytest`, `cargo test`).
- **command timeout**: 각 검증 명령에 `command_timeout`(기본 600s) 한도가 걸려 lint/build/test가 무기한 hang 하면 명확한 오류로 중단·재시도한다.
- **preflight 스냅샷**: 검증 명령 목록은 codex 실행 전에 한 번 읽어 고정한다. codex가 실행 중 `settings.json`을 바꿔 게이트를 무력화하거나 임의 명령을 심는 것을 차단한다.
- **하네스 자체 회귀**: `scripts/test_execute.py`(하네스 안전망)는 매 step 게이트에 섞지 않고 CI/수동으로 분리해 돌린다(무관한 step마다 반복 실행하지 않기 위해).

> quick/full 분리 증분 검증이 실제로 필요한 프로젝트가 생기면 그때 전용 매니페스트와 함께 도입한다(현재는 YAGNI로 두지 않음).

---

## 무결성 — 동시 실행 락 · 원자적 쓰기 (F10)

harness 상태(`index.json` 두 개)는 여러 지점에서 read-modify-write 된다. 상태가 깨지면 phase 진행이 통째로 어긋나므로 아래 **두 겹**으로 지킨다. (초기에 revision/CAS 층도 넣었으나, repo 락이 이미 harness 간 실행을 직렬화하고 step 도 순차라 CAS 가 막는 실질 시나리오가 없어 — 게다가 check→replace 사이 TOCTOU 로 진짜 CAS 도 아니라 — **의도적으로 제거**했다. 락+원자쓰기로 충분하다.)

- **상태 판정의 단일 소유자는 harness (F10c)** — 예전엔 codex 도 같은 `index.json` 을 직접 썼지만, 이제 codex 는 결과를 `HARNESS_STATUS` 로 보고만 하고 harness 가 상태를 기록한다. codex 는 여전히 `workspace-write` 라 기술적으로 `phases/**` 를 쓸 수 있으나, harness 는 codex 호출 직전 상태 파일(phase index·top index)을 스냅샷했다가 **실행 직후 복원**해 codex 의 임의 변경을 무효화한다(F10c-r). 상태 기록은 오직 harness 가 verdict 로 수행한다.
- **repo 동시 실행 락** — `run()` 시작 시 `phases/.harness.lock` 에 OS advisory lock(Windows `msvcrt` / POSIX `fcntl`)을 건다. 두 harness 가 같은 repo 의 git(브랜치 checkout·`git add -A`·commit)을 동시에 건드려 서로의 변경을 섞는 것을 막는다. 비차단이라 이미 잡혀 있으면 **fail-fast**, 프로세스가 죽으면 커널이 자동 해제해 **stale lock 이 남지 않는다**.
- **원자적 쓰기** — 모든 JSON 쓰기(`_write_json`)는 같은 디렉터리의 임시 파일에 쓴 뒤 `os.replace` 로 원자 교체한다. 쓰기 도중 크래시가 나도 `index.json` 은 항상 **이전 완전본 또는 새 완전본**만 보인다(부분 기록/손상 불가). `os.replace` 는 Windows·POSIX 모두 원자적이다. (내용 원자성 보장이며, 전원장애 시 rename durability 는 목표가 아니다 — 필요해지면 디렉터리 fsync 를 별도 요구사항으로 추가한다.)

> `phases/.harness.lock` 은 `.gitignore` 로 제외된다(로컬 런타임 산출물).

---

## 알려진 한계 (수용됨)

이 하네스는 **로컬·단일 사용자**가 실행을 지켜보며 쓰는 도구다. codex 는 적대적 공격자가 아니라 가끔 지시를 이탈하는 협력 모델로 전제한다. 아래는 "codex 가 규칙을 최악으로 어겼을 때"에만 발현하는 잔여 리스크로, **실제 사용 맥락에서 피해가 작거나 복구 가능**해 의도적으로 열어 둔다. 해당 상황이 실제로 발생하면 그때 개별 대응한다(YAGNI).

- **상태 복원 범위** — 스냅샷/복원(`_snapshot_state_files`)은 현재 phase index·top index 두 파일만 보호한다. codex 가 다른 `phases/**` 파일을 변경·생성하면 워크트리에 남을 수 있다(이력에는 안 들어감 — 커밋은 명시 pathspec, 아래 참고).
- **커밋 경계** — `_commit_step` 의 스테이징은 명시 pathspec 이라 stray `phases/**` 파일이 커밋에 섞이지 않는다. 단 `_finalize()` 의 완료 커밋은 아직 `git add -A` 를 쓴다(phase 종료 시 stray 파일이 섞일 수 있음).
- **codex 직접 git 조작** — codex 의 직접 커밋은 HEAD SHA 비교로 감지해 정규화하지만, 같은 SHA 의 다른 브랜치로 checkout 하는 경우는 감지하지 못하고 soft-reset 실패는 경고만 남긴다. blocked/error step 산출물도 `feat` 로 커밋된다(복구용, 상태는 index 로 별도 기록).
- **계획 검증** — 실행 시작 시 phase index 스키마(step 번호 유일·연속, status enum, top index 등록)를 검증하지 않는다.
- **모델 고정** — codex `--model` 을 지정하지 않아 실행 모델이 사용자 전역 설정에 의존하고 output 에 기록되지 않는다(재현성 한계).

> 보호된 것: repo 동시 실행 락, 원자적 상태 쓰기, 검증 게이트 fail-closed, timeout·**인터럽트 시 프로세스 트리 종료**(락 해제 전 자식 종료 보장), 커밋 시 코드/상태 pathspec 분리.

---

## 요구 사항
- Python 3.9+
- [codex CLI](https://github.com/openai/codex) (로그인 완료)
- git
- bash (검증 게이트가 Stop 훅의 POSIX sh 명령을 실행하는 데 사용; Windows는 Git Bash)
- (검증 게이트용) `.claude/settings.json` Stop 훅 명령 — 기본은 `npm run lint/build/test` (fail-closed, 아래 [검증 게이트 & Stop 훅](#검증-게이트--stop-훅) 참고)

## 테스트
```bash
python -m pytest scripts/test_execute.py -q
```
