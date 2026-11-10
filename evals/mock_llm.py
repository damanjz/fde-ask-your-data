"""An oracle LLM for pipeline checks: returns the gold SQL (or a refusal). Never used for reported numbers."""

from __future__ import annotations

import re

from app.llm import LLMResponse

_ROWS_RE = re.compile(r"Rows \((\d+) total\)")


class OracleLLM:
    def __init__(self, cases: list[dict]):
        self.by_question = {c["question"]: c for c in cases}
        self.calls = 0

    def chat(self, messages: list[dict]) -> LLMResponse:
        self.calls += 1
        system, user = messages[0]["content"], messages[-1]["content"]
        chars = sum(len(m["content"]) for m in messages)
        if system.startswith("You write a one-to-two sentence summary"):
            m = _ROWS_RE.search(user)
            text = f"The query returned {m.group(1)} rows." if m else "No rows."
        else:
            question = next(m["content"] for m in messages if m["role"] == "user")
            case = self.by_question.get(question)
            if case is None:
                text = "UNANSWERABLE: the oracle has no gold answer for this question."
            elif case["category"] == "refuse":
                kind = case.get("refuse_kind")
                if kind == "unanswerable":
                    text = "UNANSWERABLE: that data is not in the warehouse."
                elif kind == "pii":
                    text = "REFUSE: I cannot share personal data such as names, emails or phone numbers."
                else:
                    text = "REFUSE: I can only read the warehouse and cannot change data."
            else:
                text = f"PLAN: oracle\n```sql\n{case['sql']}\n```"
        return LLMResponse(content=text, prompt_tokens=chars // 4, completion_tokens=len(text) // 4,
                           latency_ms=1, provider="mock", model="oracle")
