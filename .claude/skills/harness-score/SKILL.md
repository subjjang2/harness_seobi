---
name: harness-score
description: >
  개발 하네스(execute.py 등 step 실행 하네스)를 codex 정적 리뷰로 감사하고, 6축(구조·맥락·계획·실행·검증·안전)으로
  점수화해 단일 HTML 스코어카드를 만든다. 패치 후 재측정 시 해결된 finding을 이력으로 남기고 델타·수정 순서를 갱신한다.
  "하네스 점수", "하네스 스코어", "하네스 채점", "하네스 리뷰 점수화", "score the harness", "harness scorecard",
  "하네스 다시 점수", "재측정", "코드리뷰 점수화" 같은 요청에서 트리거한다. 특정 하네스에 매이지 않고 어떤
  step 실행 하네스/CLI 오케스트레이터에도 적용한다. 산출물은 항상 심각도·축별로 정리된 실무형 대시보드 HTML이다.
---

# Harness Score

codex CLI로 하네스를 정적 리뷰하고 → 6축 루브릭으로 채점하고 → HTML 스코어카드를 렌더한다.
재측정 시 해결된 finding을 `resolved`로 남기고 점수 델타를 표기한다.

## 언제 쓰나

- 하네스(예: `scripts/execute.py`)의 정확성·견고성·안전성을 정량 평가하고 시각화할 때.
- 패치 후 "다시 점수 매겨줘" / "재측정" — 이전 스코어카드를 갱신(같은 파일 경로로 재발행)할 때.

## 비용 주의

codex exec를 호출한다(토큰/쿼터 소비). 전역 규칙상 유료 호출은 사전 확인이 원칙이나, 이 프로젝트는
메모리 `harness-seobi-no-cost-confirm`로 면제되어 있으면 확인 없이 진행한다. 그 외 프로젝트에서는 실행 전 알린다.

## 절차

### 1. 대상 파악
리뷰 스코프를 정한다(기본: `scripts/execute.py` + 테스트 + `.claude/` + `docs/` + `README.md`/`AGENTS.md`).
Glob/Grep으로 핵심 실행 파일과 훅/설정 위치를 먼저 좁힌다.

### 2. codex 정적 리뷰 (read-only)
프롬프트를 stdin으로 넘겨 read-only 샌드박스로 실행한다. `--json` JSONL은 인코딩 안전을 위해 반드시 파일로 저장한다.

```bash
cat <<'EOF' | codex exec -s read-only --json - 2>codex_err.log | tee codex_raw.jsonl >/dev/null
<리뷰 프롬프트: 아래 파일들을 직접 읽고 종합 코드리뷰. 관점 5가지(정확성 버그 / 백엔드 통합 약점 /
견고성 / 설계 개선 / **과잉설계·단순성**). 과잉설계 관점은 반드시 물을 것: "값어치 없는 복잡도·YAGNI·이미 다른
장치와 중복·명분 없는 재설계·이름만 안전장치(불완전 방어)는 없는가? 빼거나 되돌릴 것은?" 각 지적에 file:line
근거 + 구체적 수정안 + 심각도(High/Med/Low). 각 변경은 [유지]/[단순화]/[되돌리기]로 분류. '단순하고 옳은
설계' 관점으로 판정하고 finding 수를 늘리는 방향으로 유도하지 말 것. 한국어. 코드 수정 금지.>
EOF
python .claude/skills/harness-score/scripts/extract_codex_msg.py codex_raw.jsonl codex_review.md
```

extract 스크립트는 리뷰가 비었으면 **exit 1 + 경고**를 낸다. `codex_review.md`가 비었거나 `codex_err.log`에 에러가 있으면 **채점을 중단하고 사실대로 보고**한다 — 빈 리뷰로 점수를 지어내지 않는다(codex 미설치·쿼터소진·샌드박스 거부 등). 정상이면 `codex_review.md`를 **Read 툴로** 읽는다(터미널 직접 print는 cp949에서 한글이 깨진다).

### 3. 교차 검증
codex의 지적을 그대로 믿지 말고 코드에서 Read로 확인한다. **모든 High finding은 반드시 교차검증**한다(총점을 가장 크게 깎으므로) — CONFIRMED 못 하고 PLAUSIBLE에 그친 High는 감점 반값(−10) 또는 보류. Medium/Low는 최소 1건 이상 표본 검증. 각 finding에 CONFIRMED/PLAUSIBLE을 남긴다.

### 4. 채점
`references/rubric.md`의 축·가중치·**고정 감점**(High −20 / Med −8 / Low −3, 무결성급 High만 −25) 규칙으로 각 축 점수와 총점·등급을 계산한다.
finding을 근본 원인 기준으로 축에 매핑하고 감점을 누적한다(축 최저 30 clamp, 골격 완전 부재 축은 clamp 예외로 F 허용).

### 5. HTML 렌더
`assets/scorecard_template.html`을 참고 디자인 셸로 삼아 스코어카드를 만든다(테크니컬 콘솔 톤, 라이트/다크,
system-ui + mono, 시맨틱 3색은 액센트와 분리). 구성: hero 게이지 + 등급 + 심각도 tally → 6축 미터 →
심각도별 findings(각 file:line·수정안) → 남은 수정 순서. 게이지 offset = `452.4 × (1 − 총점/100)`.

산출물을 `reviews/harness_review.html`에 저장하고, 필요하면 Artifact로 발행한다.

### 6. 재측정(패치 후)
- 이전 스코어카드가 있으면 **같은 파일 경로로 갱신**(Artifact는 같은 URL 유지).
- 해결된 finding은 삭제하지 말고 `resolved` 처리(취소선 + 초록 pill + "패치 완료" 태그 + 적용한 수정 요약).
- 영향받은 축 점수를 재계산하고 hero에 `▲ +N (이전등급 → 새등급)` 델타 표기.
- 남은 finding으로 "수정 순서"를 갱신(싼값·즉효 → 의존성 낮은 것 → 스키마 확장류 → 통합 테스트).

## 번들 파일
- `scripts/extract_codex_msg.py` — codex JSONL → 최종 메시지 UTF-8 추출(mojibake 회피).
- `references/rubric.md` — 6축 가중치·등급 밴드·감점 규칙·재측정 규칙.
- `assets/scorecard_template.html` — 검증된 디자인 셸(스타일·구조 예시).

## 원칙
- 근거 없는 점수 금지 — 모든 감점은 `file:line` finding에 연결한다.
- 라인 번호는 리뷰 시점 기준. 패치로 이동하면 재측정 때 갱신한다.
- 스코프·가중치를 조정했으면 스코어카드 footer에 명시한다.
