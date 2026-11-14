# WOOWANGTHON_TEAM_23_AI

AI 기능 구현 레포

## 실행 방법

Python 3.12, 패키지는 uv로 관리함 (`brew install uv`).

```bash
uv sync
```

### 내부 API 서비스

BE가 호출하는 `/internal/v1` (generate, verify, health). 단답 채점은 LLM 없이 허용 답안과 글자로만 비교하므로 AI에 없음. 계약은 DOCS 레포 `api.md`를 따름.

```bash
export OPENAI_API_KEY=...   # .env.example 참고, 저장소에 커밋하지 않음
export OPENAI_MODEL=...
uv run uvicorn app.service:app --port 8001
```

- 모든 호출은 OpenAI Chat Completions로 나감. 키나 모델이 비어 있으면 `health`가 `ok: false`, 나머지는 `502 llm_unavailable`
- `health`는 OpenAI에 요청하지 않고 설정만 확인함(호출 비용 방지)
- 업로드한 자료의 글자가 OpenAI로 전송됨
- `generate`는 조각마다 OX 3개와 단답 2개를 요청함. `app/checks.py` 검사에 걸린 문제를 버리고 개수를 `discarded`로 알림. 근거 문장이 원문과 조금 다른 경우(`evidence_edited`)만 통과시킴
- OX는 모델이 정답을 O로 몰아 내는 경향이 있어(평가에서 O 69, X 12) 참 문장과 사실 하나를 바꾼 거짓 문장을 함께 받고, 서버가 둘 중 하나를 무작위로 골라 정답 O/X를 정함
- `generate`의 `avoid`(선택): 이 자료로 이미 낸 문제 목록. 프롬프트에 넣어 같은 내용을 피하게 하고, 글자까지 같은 문제는 응답에서 뺌. BE가 새 문제를 채울 때 씀

테스트 (LLM 응답을 테스트 안에서 정해 둠):

```bash
uv run pytest
```

### 모델 비교 평가 (로컬 Ollama, 이전 선정 과정 기록)

서비스는 OpenAI만 쓰고, 아래 평가 코드는 초기 모델 선정에 쓴 Ollama용이라 그대로 둠.

모델은 Ollama로 받음.

```bash
ollama pull gemma3:12b
ollama pull qwen3.5:9b
ollama pull exaone3.5:7.8b
```

샘플 PDF는 한국어 위키백과(CC BY-SA 4.0)라 레포에 포함하지 않음. `bench/samples/`에 받음.

```bash
mkdir -p bench/samples
for t in 운영_체제 관계형_데이터베이스 미분; do
  curl -L -A "WoowangthonTeam23/0.1 (https://github.com/woowangthon/WOOWANGTHON_TEAM_23_AI)" \
    -o "bench/samples/$t.pdf" "https://ko.wikipedia.org/api/rest_v1/page/pdf/$t"
done
```

실행:

```bash
uv run python -m bench.run --models gemma3:12b qwen3.5:9b exaone3.5:7.8b
```

결과는 `bench/results/<시각>/`에 저장됨.

- `summary.md`: 모델별 속도, 자동 검사 결함 비율
- `review.csv`: 모델 이름을 가린 채점용 문제. 팀이 정답 여부를 직접 채점함
- `review_key.csv`: 채점 번호와 모델의 대응. 채점이 끝난 뒤에 열 것

### Ollama 주의

- 서버 기본 컨텍스트는 4096 토큰(이 Mac 기준). 입력이 넘으면 조용히 절반 정도로 잘림. 평가 조각을 2000자로 맞췄고, 잘림은 `bench.run`이 표시함
- 컨텍스트는 서버 환경변수 `OLLAMA_CONTEXT_LENGTH`로 올림
- qwen3.5는 OpenAI 호환 주소에서 추론이 기본으로 켜져 느림. `reasoning_effort: "none"`으로 끔 (`bench/run.py`)
