import json

import pytest
from fastapi.testclient import TestClient

from app import service
from app.llm import LLMError

SOURCE = "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다. 장치 드라이버의 주된 설계 목적은 추상화이다."


@pytest.fixture
def llm(monkeypatch):
    """LLM 응답을 정해 두고 받은 프롬프트를 기록함"""
    state = {"reply": {}, "prompts": [], "fail": False}

    def fake(providers, messages):
        state["prompts"].append(messages[0]["content"])
        if state["fail"]:
            raise LLMError("down")
        return {"provider": "openai", "content": json.dumps(state["reply"], ensure_ascii=False)}

    monkeypatch.setattr(service, "chat_json_fallback", fake)
    return state


client = TestClient(service.app)


def test_generate_keeps_valid_and_discards_rest(llm):
    llm["reply"] = {"questions": [
        {"type": "ox", "question": "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다.", "answer": "O",
         "evidence": "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다."},
        {"type": "short", "question": "장치 드라이버의 주된 설계 목적은 무엇인가?", "answer": "추상화", "accepted": ["추상화", " "],
         "evidence": "장치 드라이버의 주된 설계 목적은 추상화이다."},
        {"type": "short", "question": "드라이버 목적은 무엇인가?", "answer": "추상화", "accepted": ["추상화"],
         "evidence": "원문에 없는 문장을 지어냄."},
        "문자열",
    ]}
    r = client.post("/internal/v1/generate", json={"text": SOURCE}).json()
    assert r["discarded"] == 1
    assert r["provider"] == "openai"
    assert [q["type"] for q in r["questions"]] == ["ox", "short"]
    assert r["questions"][0]["accepted"] == []
    assert r["questions"][1]["accepted"] == ["추상화"]
    assert SOURCE in llm["prompts"][0]


def test_generate_handles_broken_json(llm):
    llm["reply"] = {"items": []}
    r = client.post("/internal/v1/generate", json={"text": SOURCE}).json()
    assert r == {"questions": [], "provider": "openai", "discarded": 0}


def test_llm_failure_returns_502(llm):
    llm["fail"] = True
    r = client.post("/internal/v1/generate", json={"text": SOURCE})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "llm_unavailable"



def test_verify_compares_answer(llm):
    q = {"type": "short", "question": "장치 드라이버의 주된 설계 목적은 무엇인가?", "answer": "추상화", "accepted": []}
    llm["reply"] = {"answer": "추상화", "reason": "근거 문장에 있음"}
    assert client.post("/internal/v1/verify", json={"question": q, "source": SOURCE}).json()["supported"] is True
    llm["reply"] = {"answer": "", "reason": "정할 수 없음"}
    assert client.post("/internal/v1/verify", json={"question": q, "source": SOURCE}).json()["supported"] is False


def test_health_and_missing_key(monkeypatch):
    monkeypatch.setattr(service, "providers", [])
    assert client.get("/internal/v1/health").json() == {"ok": False, "provider": None}
    r = client.post("/internal/v1/generate", json={"text": SOURCE})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "llm_unavailable"


def test_avoid_list_goes_into_prompt_and_duplicates_are_dropped(llm):
    llm["reply"] = {"questions": [
        {"type": "ox", "question": "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다.", "answer": "O",
         "evidence": "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다."},
        {"type": "short", "question": "장치 드라이버의 주된 설계 목적은 무엇인가?", "answer": "추상화", "accepted": ["추상화"],
         "evidence": "장치 드라이버의 주된 설계 목적은 추상화이다."},
    ]}
    r = client.post("/internal/v1/generate", json={
        "text": SOURCE, "avoid": ["운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다.", " "],
    }).json()
    assert [q["type"] for q in r["questions"]] == ["short"]
    assert r["discarded"] == 1
    prompt = llm["prompts"][0]
    assert "이미 낸 문제" in prompt and "- 운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다." in prompt
    assert prompt.index("이미 낸 문제") < prompt.index(SOURCE)
    client.post("/internal/v1/generate", json={"text": SOURCE})
    assert "이미 낸 문제" not in llm["prompts"][1]


def test_ox_pair_becomes_true_or_false_question(llm, monkeypatch):
    pair = {"type": "ox", "true": "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다.",
            "false": "운영체제는 응용 프로그램과 사용자 사이의 인터페이스 역할을 한다.",
            "evidence": "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다."}
    llm["reply"] = {"questions": [pair]}
    monkeypatch.setattr(service.random, "random", lambda: 0.1)
    q = client.post("/internal/v1/generate", json={"text": SOURCE}).json()["questions"][0]
    assert (q["question"], q["answer"]) == (pair["true"], "O")
    monkeypatch.setattr(service.random, "random", lambda: 0.9)
    q = client.post("/internal/v1/generate", json={"text": SOURCE}).json()["questions"][0]
    assert (q["question"], q["answer"]) == (pair["false"], "X")
    assert "true" not in q and "false" not in q


def test_ox_pair_without_real_change_is_discarded(llm):
    same = "운영체제는 응용 프로그램과 하드웨어 사이의 인터페이스 역할을 한다."
    llm["reply"] = {"questions": [
        {"type": "ox", "true": same, "false": same + " ", "evidence": same},
        {"type": "ox", "true": same, "false": "", "evidence": same},
    ]}
    r = client.post("/internal/v1/generate", json={"text": SOURCE}).json()
    assert r["questions"] == [] and r["discarded"] == 2
