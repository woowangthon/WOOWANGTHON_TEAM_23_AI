import json
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.checks import check_question, squash
from app.llm import LLMError, Provider, chat_json_fallback
from app.prompts import ADVICE_PROMPT, VERIFY_PROMPT, question_prompt

# 키와 모델은 환경변수로만 받음. 둘 중 하나라도 비면 호출 없이 llm_unavailable로 응답함
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
# 원문과 조금 달라도 BE가 쪽수를 찾을 수 있는 수준이라 이 결함만 허용함
ALLOWED_ISSUES = {"evidence_edited"}

# temperature는 보내지 않음. 추론 계열 모델은 기본값 외 값을 거부함
providers = (
    [Provider(name="openai", base_url="https://api.openai.com/v1", model=OPENAI_MODEL, api_key=OPENAI_API_KEY, temperature=None)]
    if OPENAI_API_KEY and OPENAI_MODEL
    else []
)

app = FastAPI()


def ask(prompt: str) -> tuple[dict, str]:
    try:
        result = chat_json_fallback(providers, [{"role": "user", "content": prompt}])
    except LLMError as e:
        raise HTTPException(502, detail={"code": "llm_unavailable", "message": str(e)[:300]})
    try:
        data = json.loads(result["content"])
    except ValueError:
        data = {}
    return (data if isinstance(data, dict) else {}), result["provider"]


@app.exception_handler(HTTPException)
def http_error(_, e: HTTPException):
    detail = e.detail if isinstance(e.detail, dict) else {"code": "invalid_request", "message": str(e.detail)}
    return JSONResponse({"error": detail}, status_code=e.status_code)


MAX_AVOID = 30


class GenerateRequest(BaseModel):
    text: str
    # 이 자료로 이미 낸 문제. 같은 내용을 다시 묻지 않기 위해 프롬프트에 넣음
    avoid: list[str] = []


@app.post("/internal/v1/generate")
def generate(req: GenerateRequest):
    avoid = [a.strip()[:200] for a in req.avoid if a.strip()][:MAX_AVOID]
    data, provider = ask(question_prompt(avoid) + req.text)
    items = data.get("questions")
    items = [q for q in items if isinstance(q, dict)] if isinstance(items, list) else []
    kept = []
    seen = {squash(a) for a in avoid}
    for q in items:
        if not str(q.get("question", "")).strip() or set(check_question(q, req.text)) - ALLOWED_ISSUES:
            continue
        # 프롬프트로 막아도 같은 문제가 나올 수 있어 글자까지 같으면 버림
        if squash(str(q["question"])) in seen:
            continue
        seen.add(squash(str(q["question"])))
        kept.append({
            "type": q["type"],
            "question": str(q["question"]).strip(),
            "answer": str(q["answer"]).strip(),
            "accepted": [str(a).strip() for a in q.get("accepted") or [] if str(a).strip()] if q["type"] == "short" else [],
            "evidence": str(q["evidence"]).strip(),
        })
    return {"questions": kept, "provider": provider, "discarded": len(items) - len(kept)}


class Question(BaseModel):
    type: str
    question: str
    answer: str
    accepted: list[str] = []
    evidence: str = ""


def normalize(text: str) -> str:
    return squash(text).casefold()


def matches(user_answer: str, answer: str, accepted: list[str]) -> bool:
    return normalize(user_answer) in {normalize(a) for a in [answer, *accepted] if a.strip()}


class VerifyRequest(BaseModel):
    question: Question
    source: str


@app.post("/internal/v1/verify")
def verify(req: VerifyRequest):
    q = req.question
    data, _ = ask(VERIFY_PROMPT.format(type=q.type, question=q.question, source=req.source))
    solved = str(data.get("answer", "")).strip()
    supported = bool(solved) and matches(solved, q.answer, q.accepted)
    reason = str(data.get("reason", "")).strip()
    return {"supported": supported, "reason": f"다시 푼 답: {solved or '없음'}. {reason}".strip()}



NUMBER = re.compile(r"\d+(?:\.\d+)?")


class AdviceRequest(BaseModel):
    facts: dict
    suggestion: dict | None = None


def fallback_message(facts: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in facts.items())


@app.post("/internal/v1/advice")
def advice(req: AdviceRequest):
    facts_text = json.dumps(req.facts, ensure_ascii=False)
    data, _ = ask(ADVICE_PROMPT.format(facts=facts_text, suggestion=json.dumps(req.suggestion or {}, ensure_ascii=False)))
    message = str(data.get("message", "")).strip()
    allowed = set(NUMBER.findall(facts_text + json.dumps(req.suggestion or {}, ensure_ascii=False)))
    # 입력에 없는 숫자가 문장에 있으면 버리고 수치만 나열함
    if not message or not set(NUMBER.findall(message)) <= allowed:
        return {"message": fallback_message(req.facts), "fallback": True}
    return {"message": message, "fallback": False}


@app.get("/internal/v1/health")
def health():
    # 호출 비용이 들어 OpenAI에는 요청하지 않고 설정만 확인함
    return {"ok": bool(providers), "provider": providers[0].name if providers else None}
