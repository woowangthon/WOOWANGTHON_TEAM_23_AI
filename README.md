# WOOWANGTHON_TEAM_23_AI

AI 기능 구현 레포

## 실행 방법

Python 3.12, 패키지는 uv로 관리함 (`brew install uv`).

```bash
uv sync
```

### 내부 API 서비스

BE가 호출하는 `/internal/v1` (generate, verify, advice, health). 단답 채점은 LLM 없이 허용 답안과 글자로만 비교하므로 AI에 없음. 계약은 DOCS 레포 `api.md`를 따름.

```bash
ollama pull qwen3.5:9b
OLLAMA_CONTEXT_LENGTH=8192 ollama serve
uv run uvicorn app.service:app --port 8001
```

- Ollama 기본 컨텍스트(4096)에서는 긴 입력이 오류 없이 잘려서 8192로 띄움
- `OLLAMA_UNLOAD_EVERY`(기본 10): 호출이 쌓이면 qwen3.5 메모리가 계속 늘어 이 횟수마다 모델을 내림
- `OPENAI_API_KEY`, `OPENAI_MODEL`: 둘 다 있을 때만 Ollama 실패 시 OpenAI로 넘어감. 지금은 비워 둠
- `generate`는 `app/checks.py` 검사에 걸린 문제를 버리고 개수를 `discarded`로 알림. 근거 문장이 원문과 조금 다른 경우(`evidence_edited`)만 통과시킴

테스트 (LLM 응답을 테스트 안에서 정해 둠):

```bash
uv run pytest
```

### 모델 비교 평가

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
