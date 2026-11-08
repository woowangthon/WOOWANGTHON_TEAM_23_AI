import argparse
import csv
import json
import random
import statistics
import time
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from app.llm import LLMError, Provider, chat_json
from app.prompts import QUESTION_PROMPT
from app.checks import check_question

CHUNK_CHARS = 2000
OLLAMA_BASE_URL = "http://localhost:11434/v1"
EXTRA_BODY = {"qwen3.5": {"reasoning_effort": "none"}}  # 모델 이름이 이 접두사로 시작하면 적용
ISSUES = [
    "evidence_missing", "evidence_edited", "evidence_not_in_source", "answer_leak", "refers_to_source", "ox_answer_invalid",
    "ox_not_statement", "answer_missing", "short_answer_ox", "short_answer_sentence", "short_not_question",
    "short_no_accepted", "answer_in_question", "unknown_type",
]


def extra_body_for(model: str) -> dict:
    return next((v for k, v in EXTRA_BODY.items() if model.startswith(k)), {})


def chunks(pdf: Path, count: int) -> list[str]:
    if pdf.suffix == ".txt":
        text = pdf.read_text(encoding="utf-8")
    else:
        text = "\n".join((page.extract_text() or "") for page in PdfReader(pdf).pages)
    body = text[: int(len(text) * 0.7)]  # 뒤쪽은 각주·참고문헌이라 제외
    pieces = [body[i : i + CHUNK_CHARS] for i in range(0, len(body), CHUNK_CHARS)]
    pieces = [p for p in pieces if len(p) > CHUNK_CHARS // 2]
    if len(pieces) <= count:
        return pieces
    step = len(pieces) / count
    return [pieces[int(i * step)] for i in range(count)]


def run_model(model: str, samples: list[tuple[str, int, str]], base_url: str = OLLAMA_BASE_URL) -> tuple[list[dict], list[dict]]:
    provider = Provider(name=model, base_url=base_url, model=model, extra_body=extra_body_for(model))
    calls, questions = [], []
    for pdf, index, source in samples:
        prompt = QUESTION_PROMPT + source
        call = {"model": model, "pdf": pdf, "chunk": index, "error": None, "json_ok": False, "truncated": False}
        calls.append(call)
        try:
            result = chat_json(provider, [{"role": "user", "content": prompt}])
        except LLMError as e:
            call["error"] = str(e)[:160]
            continue
        call.update(seconds=result["seconds"], completion_tokens=result["completion_tokens"])
        # 입력이 컨텍스트를 넘으면 Ollama가 조용히 잘라냄. 한글은 토큰당 약 1.5자라 3자 미만이면 잘린 것으로 봄
        call["truncated"] = result["prompt_tokens"] < len(prompt) / 3
        try:
            items = json.loads(result["content"])["questions"]
            call["json_ok"] = True
        except (ValueError, KeyError, TypeError):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            questions.append({
                "model": model, "pdf": pdf, "chunk": index, "type": item.get("type"),
                "question": item.get("question", ""), "answer": item.get("answer", ""),
                "accepted": item.get("accepted"), "evidence": item.get("evidence", ""),
                "issues": check_question(item, source),
            })
    return calls, questions


def summarize(models: list[str], calls: list[dict], questions: list[dict]) -> str:
    lines = [
        "| 모델 | 호출 | 실패 | JSON 오류 | 잘림 | 문제 수(기대치 대비) | 호출당 초(중앙값) | 초당 토큰(중앙값) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model in models:
        c = [x for x in calls if x["model"] == model]
        done = [x for x in c if "seconds" in x]
        seconds = [x["seconds"] for x in done]
        speed = [x["completion_tokens"] / x["seconds"] for x in done if x["seconds"] > 0]
        q = [x for x in questions if x["model"] == model]
        median_seconds = statistics.median(seconds) if seconds else 0
        median_speed = statistics.median(speed) if speed else 0
        lines.append(
            f"| {model} | {len(c)} | {sum(bool(x['error']) for x in c)} | "
            f"{sum(not x['json_ok'] and not x['error'] for x in c)} | {sum(x['truncated'] for x in c)} | "
            f"{len(q)}/{2 * sum(x['json_ok'] for x in c)} | "
            f"{median_seconds:.1f} | {median_speed:.1f} |"
        )
    lines += ["", "문제 결함 비율(문제 수 대비)", "", "| 모델 | " + " | ".join(ISSUES) + " | 결함 없음 |", "|---" * (len(ISSUES) + 2) + "|"]
    for model in models:
        q = [x for x in questions if x["model"] == model]
        if not q:
            lines.append(f"| {model} | 문제 없음 |")
            continue
        counts = Counter(i for x in q for i in x["issues"])
        clean = sum(not x["issues"] for x in q)
        cells = [f"{counts[i] / len(q):.0%}" for i in ISSUES]
        lines.append(f"| {model} | " + " | ".join(cells) + f" | {clean / len(q):.0%} |")
    return "\n".join(lines)


def write_review(questions: list[dict], count: int, out: Path) -> None:
    """모델 이름을 가리고 섞어 팀이 정답 여부를 직접 채점하게 함"""
    rng = random.Random(0)
    models = sorted({q["model"] for q in questions})
    picked = []
    for model in models:
        pool = [q for q in questions if q["model"] == model]
        picked += rng.sample(pool, min(count // len(models), len(pool)))
    rng.shuffle(picked)
    with open(out / "review.csv", "w", newline="", encoding="utf-8-sig") as f, \
            open(out / "review_key.csv", "w", newline="", encoding="utf-8-sig") as k:
        w, key = csv.writer(f), csv.writer(k)
        w.writerow(["id", "문서", "유형", "문제", "정답", "허용 답안", "근거 문장", "정답이 맞나(O/X)", "문제로 쓸 만한가(O/X)", "비고"])
        key.writerow(["id", "model"])
        for n, q in enumerate(picked, 1):
            accepted = ", ".join(map(str, q["accepted"])) if isinstance(q["accepted"], list) else ""
            w.writerow([n, q["pdf"], q["type"], q["question"], q["answer"], accepted, q["evidence"], "", "", ""])
            key.writerow([n, q["model"]])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--pdf-dir", default="bench/samples")
    parser.add_argument("--per-pdf", type=int, default=10)
    parser.add_argument("--review", type=int, default=24)
    parser.add_argument("--out", default="bench/results")
    # OpenAI 호환 서버 주소. 기본은 Ollama
    parser.add_argument("--base-url", default=OLLAMA_BASE_URL)
    args = parser.parse_args()

    pdfs = sorted(p for p in Path(args.pdf_dir).iterdir() if p.suffix in (".pdf", ".txt"))
    samples = [(pdf.stem, i, source) for pdf in pdfs for i, source in enumerate(chunks(pdf, args.per_pdf))]
    print(f"조각 {len(samples)}개, 모델 {len(args.models)}개", flush=True)

    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    all_calls, all_questions = [], []
    # "모델@주소"로 모델마다 다른 서버를 쓸 수 있음. 예: qwen3.5:9b other-model@http://localhost:8081/v1
    specs = [m.partition("@") for m in args.models]
    models = [model for model, _, _ in specs]
    for model, _, url in specs:
        calls, questions = run_model(model, samples, url or args.base_url)
        all_calls += calls
        all_questions += questions
        print(f"{model}: 호출 {len(calls)}, 문제 {len(questions)}", flush=True)

    with open(out / "questions.jsonl", "w", encoding="utf-8") as f:
        for q in all_questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    summary = summarize(models, all_calls, all_questions)
    (out / "summary.md").write_text(summary + "\n", encoding="utf-8")
    write_review(all_questions, args.review, out)
    print(summary)
    print(f"\n결과: {out}")


if __name__ == "__main__":
    main()
