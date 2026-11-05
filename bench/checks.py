import difflib
import re

EVIDENCE_EDITED_BELOW = 1.0
EVIDENCE_FABRICATED_BELOW = 0.8
SOURCE_REFERENCE = re.compile(r"자료에서|본문에서|본문에|언급된|위 글|다음 글|위 내용|제시된")
LEAK = re.compile(r"\((?:O|X|정답)\)|정답\s*[:：]")
INTERROGATIVE = re.compile(r"무엇|누구|어느|몇|어떤|왜")


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def evidence_ratio(evidence: str, source: str) -> float:
    """근거 문장 글자 중 원문과 겹치는 비율. PDF 추출 잡음 때문에 완전 일치만 보면 과하게 걸러냄"""
    e, s = squash(evidence), squash(source)
    if not e:
        return 0.0
    blocks = difflib.SequenceMatcher(None, s, e, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(e)


def check_question(q: dict, source: str) -> list[str]:
    """자동으로 가릴 수 있는 결함만 검사함. 정답이 맞는지는 판정하지 못함"""
    issues = []
    text = str(q.get("question", ""))
    evidence = str(q.get("evidence", ""))
    answer = str(q.get("answer", ""))

    if not evidence.strip():
        issues.append("evidence_missing")
    else:
        ratio = evidence_ratio(evidence, source)
        if ratio < EVIDENCE_FABRICATED_BELOW:
            issues.append("evidence_not_in_source")
        elif ratio < EVIDENCE_EDITED_BELOW:
            issues.append("evidence_edited")
    if LEAK.search(text):
        issues.append("answer_leak")
    if SOURCE_REFERENCE.search(text):
        issues.append("refers_to_source")

    if q.get("type") == "ox":
        if answer not in ("O", "X"):
            issues.append("ox_answer_invalid")
        if text.rstrip().endswith("?") or INTERROGATIVE.search(text):
            issues.append("ox_not_statement")
    elif q.get("type") == "short":
        if not answer.strip():
            issues.append("answer_missing")
        accepted = q.get("accepted")
        if not isinstance(accepted, list) or not any(str(a).strip() for a in accepted):
            issues.append("short_no_accepted")
        if answer and squash(answer) in squash(text):
            issues.append("answer_in_question")
    else:
        issues.append("unknown_type")
    return issues
