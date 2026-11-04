import argparse
import asyncio
import re
import subprocess
import time
from pathlib import Path

import httpx

from app.llm import Provider, chat_json
from app.prompts import QUESTION_PROMPT
from bench.run import chunks, extra_body_for

DOCS = 5
CHUNKS_PER_DOC = 5


def shell(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def swap_used_mb() -> float:
    match = re.search(r"used = ([\d.]+)M", shell("sysctl", "-n", "vm.swapusage"))
    return float(match.group(1)) if match else 0.0


def free_percent() -> int:
    match = re.search(r"free percentage: (\d+)%", shell("memory_pressure"))
    return int(match.group(1)) if match else -1


async def sample(server: str, stop: asyncio.Event, model_bytes: list[int], free: list[int]) -> None:
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                models = (await client.get(f"{server}/api/ps", timeout=3)).json().get("models", [])
                model_bytes.append(sum(m["size"] for m in models))
            except httpx.HTTPError:
                pass
            free.append(await asyncio.to_thread(free_percent))
            await asyncio.sleep(2)


class Unloader:
    """N회 호출마다 모델을 내려 Ollama가 쌓은 메모리를 비움"""

    def __init__(self, server: str, model: str, every: int):
        self.server, self.model, self.every = server, model, every
        self.calls = 0
        self.lock = asyncio.Lock()

    async def after_call(self) -> None:
        self.calls += 1
        if not self.every or self.calls % self.every:
            return
        async with self.lock:
            async with httpx.AsyncClient() as client:
                await client.post(f"{self.server}/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=120)


async def run_doc(provider: Provider, prompts: list[str], started: float, done: dict, index: int,
                  unloader: Unloader) -> int:
    errors = 0
    for n, prompt in enumerate(prompts):
        try:
            await asyncio.to_thread(chat_json, provider, [{"role": "user", "content": prompt}])
        except Exception:
            errors += 1
        await unloader.after_call()
        if n == 0:
            done[f"doc{index}_first"] = time.monotonic() - started
    done[f"doc{index}_done"] = time.monotonic() - started
    return errors


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:11434")
    parser.add_argument("--model", required=True)
    parser.add_argument("--pdf-dir", default="bench/samples")
    parser.add_argument("--unload-every", type=int, default=0)
    args = parser.parse_args()

    pdfs = sorted(Path(args.pdf_dir).glob("*.pdf"))
    pools = [chunks(p, 10) for p in pdfs]
    # 자료 5개: 같은 PDF의 서로 다른 조각 구간을 다른 자료처럼 사용함
    docs = []
    for i in range(DOCS):
        pool = pools[i % len(pools)]
        offset = (i // len(pools)) * CHUNKS_PER_DOC
        docs.append([QUESTION_PROMPT + c for c in (pool[offset:] + pool)[:CHUNKS_PER_DOC]])

    provider = Provider(name=args.model, base_url=f"{args.server}/v1", model=args.model,
                        extra_body=extra_body_for(args.model), timeout=900)
    swap_before = swap_used_mb()
    model_bytes, free = [], []
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample(args.server, stop, model_bytes, free))

    started = time.monotonic()
    done: dict = {}
    unloader = Unloader(args.server, args.model, args.unload_every)
    errors = await asyncio.gather(*(run_doc(provider, d, started, done, i, unloader) for i, d in enumerate(docs)))
    wall = time.monotonic() - started
    stop.set()
    await sampler

    finish = sorted(done[f"doc{i}_done"] for i in range(DOCS))
    first = sorted(done[f"doc{i}_first"] for i in range(DOCS))
    print(
        f"model={args.model} calls={DOCS * CHUNKS_PER_DOC} errors={sum(errors)} wall={wall:.0f}s "
        f"firstQuestion={first[0]:.0f}~{first[-1]:.0f}s docDone={finish[0]:.0f}~{finish[-1]:.0f}s "
        f"modelMemMax={max(model_bytes, default=0) / 1e9:.1f}GB "
        f"swapDelta={swap_used_mb() - swap_before:+.0f}MB minFree={min(free, default=-1)}%"
    )


if __name__ == "__main__":
    asyncio.run(main())
