import json
import os
import re
import threading

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.checks import check_question, squash
from app.llm import LLMError, Provider, chat_json_fallback
from app.prompts import ADVICE_PROMPT, QUESTION_PROMPT, VERIFY_PROMPT

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
# 키가 비어 있으면 폴백 없이 Ollama만 씀
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")
# qwen3.5는 호출이 쌓일수록 메모리가 늘어 일정 횟수마다 내렸다 올림 (노션 실험 기록 5)
UNLOAD_EVERY = int(os.environ.get("OLLAMA_UNLOAD_EVERY", "10"))
# 원문과 조금 달라도 BE가 쪽수를 찾을 수 있는 수준이라 이 결함만 허용함
ALLOWED_ISSUES = {"evidence_edited"}

ollama = Provider(name="ollama", base_url=OLLAMA_BASE_URL, model=OLLAMA_MODEL, extra_body={"reasoning_effort": "none"})
providers = [ollama]
if OPENAI_API_KEY and OPENAI_MODEL:
    providers.append(Provider(name="openai", base_url="https://api.openai.com/v1", model=OPENAI_MODEL, api_key=OPENAI_API_KEY))

calls = 0
calls_lock = threading.Lock()

app = FastAPI()


def unload_ollama() -> None:
    try:
        httpx.post(f"{OLLAMA_BASE_URL.removesuffix('/v1')}/api/generate", json={"model": OLLAMA_MODEL, "keep_alive": 0}, timeout=30)
    except httpx.HTTPError:
        pass


def ask(prompt: str) -> tuple[dict, str]:
    global calls
    try:
        result = chat_json_fallback(providers, [{"role": "user", "content": prompt}])
    except LLMError as e:
        raise HTTPException(502, detail={"code": "llm_unavailable", "message": str(e)[:300]})
    if result["provider"] == "ollama":
        with calls_lock:
            calls += 1
            unload = calls % UNLOAD_EVERY == 0
        if unload:
            unload_ollama()
    try:
        data = json.loads(result["content"])
    except ValueError:
        data = {}
    return (data if isinstance(data, dict) else {}), result["provider"]


@app.exception_handler(HTTPException)
def http_error(_, e: HTTPException):
    detail = e.detail if isinstance(e.detail, dict) else {"code": "invalid_request", "message": str(e.detail)}
    return JSONResponse({"error": detail}, status_code=e.status_code)


class GenerateRequest(BaseModel):
    text: str


@app.post("/internal/v1/generate")
def generate(req: GenerateRequest):
    data, provider = ask(QUESTION_PROMPT + req.text)
    items = data.get("questions")
    items = [q for q in items if isinstance(q, dict)] if isinstance(items, list) else []
    kept = []
    for q in items:
        if not str(q.get("question", "")).strip() or set(check_question(q, req.text)) - ALLOWED_ISSUES:
            continue
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
    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/models", timeout=5)
        r.raise_for_status()
        return {"ok": True, "provider": "ollama"}
    except httpx.HTTPError:
        if len(providers) > 1:
            return {"ok": True, "provider": providers[1].name}
        return {"ok": False, "provider": None}
