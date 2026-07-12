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
- `feat-<task>` 브랜치 생성/checkout
- 가드레일 주입 (CLAUDE.md + docs)
- 컨텍스트 누적 (완료 step의 summary를 다음 프롬프트에 전달)
- 자가 교정 (실패 시 최대 3회 재시도, 이전 에러를 프롬프트에 피드백)
- **검증 게이트** (lint/build/test 통과해야 step 완료 인정)
- 2단계 커밋 (코드 `feat` / 메타데이터 `chore` 분리)
- 타임스탬프 자동 기록

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
[ -f package.json ] || exit 0; npm run lint 2>&1 && npm run build 2>&1 && npm run test 2>&1
```

- **`[ -f package.json ] || exit 0` 가드**: package.json이 없는 상태(빈 템플릿 등)에선 검증을 조용히 통과(no-op)시켜, npm 에러 노이즈를 없앤다. 실제 프로젝트가 되면 그대로 lint/build/test를 검증한다.
- 명령은 POSIX sh 문법이므로 `execute.py`는 이를 **bash로 실행**한다 (Windows `cmd.exe`가 `[ -f ]`·`;`를 못 파싱하는 문제 회피, bash 없으면 shell fallback).
- 다른 스택이면 이 명령만 프로젝트에 맞게 바꾸면 된다 (예: `pytest`, `cargo test`).

---

## 요구 사항
- Python 3.9+
- [codex CLI](https://github.com/openai/codex) (로그인 완료)
- git
- bash (검증 게이트가 Stop 훅의 POSIX sh 명령을 실행하는 데 사용; Windows는 Git Bash)
- (검증 게이트용) `.claude/settings.json` Stop 훅 명령 — 기본은 package.json 가드 + `npm run lint/build/test` (아래 [검증 게이트 & Stop 훅](#검증-게이트--stop-훅) 참고)

## 테스트
```bash
python -m pytest scripts/test_execute.py -q
```
