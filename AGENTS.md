# AGENTS.md

이 파일은 codex가 레포 루트에서 자동으로 읽는 프로젝트 지침이다.
프로젝트 규칙의 **단일 출처는 `CLAUDE.md`와 `docs/`** 이며, 하네스 실행 시
`scripts/execute.py`가 그 내용을 매 step 프롬프트에 주입한다.

## 반드시 먼저 읽을 것

- `CLAUDE.md` — 기술 스택, 아키텍처 CRITICAL 규칙, 개발 프로세스(TDD)
- `docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/ADR.md`, `docs/UI_GUIDE.md`

## 작업 규칙 (요약)

- CRITICAL: 새 기능은 테스트를 먼저 작성하고 통과하는 구현을 작성한다 (TDD).
- 커밋 메시지는 conventional commits 형식(feat:, fix:, docs:, refactor:).
- 하네스 step 실행 중에는 해당 step에 명시된 작업만 수행하고, `phases/<task>/index.json`의
  step status를 규칙대로 업데이트한다 (completed/error/blocked). 상세는 주입되는 프롬프트 참조.

## 검증

step 완료 후 `execute.py`가 `.claude/settings.json`의 Stop 훅 명령(lint·build·test)을
게이트로 실행한다. 검증을 통과해야 step이 완료로 인정된다.
