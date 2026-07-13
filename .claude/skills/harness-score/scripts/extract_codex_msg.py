#!/usr/bin/env python3
"""codex exec --json 출력(JSONL)에서 최종 assistant 메시지를 UTF-8로 안전 추출한다.

Windows 콘솔(cp949)로 직접 print 하면 한글이 깨지므로, 반드시 파일로 저장해서 읽는다.

Usage:
    python extract_codex_msg.py <raw.jsonl> <out.md>
"""
import json
import sys


def extract(raw_path: str) -> str:
    msgs = []
    with open(raw_path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                event = json.loads(ln)
            except json.JSONDecodeError:
                continue

            def walk(o):
                if isinstance(o, dict):
                    # codex 0.14x: item.completed -> item.{assistant_message|agent_message}.text
                    if o.get("type") == "item.completed":
                        it = o.get("item", {})
                        if it.get("type") in ("assistant_message", "agent_message") and it.get("text"):
                            msgs.append(it["text"])
                    # 구버전 호환: agent_message.message(str)
                    if o.get("type") in ("agent_message", "assistant") and isinstance(o.get("message"), str):
                        msgs.append(o["message"])
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)

            walk(event)
    # codex가 ack + 본문을 별도 completed item으로 흘리면 마지막이 본문이 아닐 수 있다.
    # 리뷰 본문은 가장 긴 메시지라는 가정으로 최장 메시지를 고른다.
    return max(msgs, key=len) if msgs else ""


def main():
    if len(sys.argv) != 3:
        print("Usage: python extract_codex_msg.py <raw.jsonl> <out.md>", file=sys.stderr)
        sys.exit(2)
    text = extract(sys.argv[1])
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(text)
    if not text.strip():
        # 빈 리뷰로 채점하는 사고를 막는다: 호출 측이 중단하도록 실패 코드를 낸다.
        print(
            f"ERROR: no assistant message extracted from {sys.argv[1]} "
            "(codex 미설치·쿼터소진·샌드박스 거부 가능). 채점을 중단하고 codex_err.log를 확인하라.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"extracted {len(text)} chars -> {sys.argv[2]}")


if __name__ == "__main__":
    main()
