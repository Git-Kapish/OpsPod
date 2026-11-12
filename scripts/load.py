"""Tiny load generator: posts golden-set cases to a running API (drills and demos).

uv run python scripts/load.py --rate 1 --duration 120 --ambiguous 0.8
"""

import argparse
import asyncio
import collections
import json
import random
import time
import uuid
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parent.parent / "evals" / "datasets"


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for f in sorted(DATA.glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
        if line
    ]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", default="dev-agent")
    ap.add_argument("--rate", type=float, default=1.0, help="cases per second")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--ambiguous", type=float, default=0.1, help="share of LLM-path cases")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = random.Random(args.seed)  # noqa: S311 - load shape only
    cases = load_cases()
    amb = [c for c in cases if "ambiguous" in c["tags"] and "injection" not in c["tags"]]
    rest = [c for c in cases if c not in amb]
    stats: collections.Counter[str] = collections.Counter()
    end = time.monotonic() + args.duration
    async with httpx.AsyncClient(base_url=args.url, timeout=10) as client:
        while time.monotonic() < end:
            case = rng.choice(amb) if rng.random() < args.ambiguous else rng.choice(rest)
            i = case["input"]
            body = {
                "external_id": i["external_id"],
                "case_type": case["case_type"],
                "region": case["region"],
                "rider_ref": f"{i['rider_ref']}-{uuid.uuid4().hex[:8]}",
                # unique suffix defeats the gateway prompt cache so provider load is realistic
                "complaint_text": f"{i['complaint_text']} (ref {uuid.uuid4().hex[:6]})",
            }
            try:
                r = await client.post(
                    "/v1/cases",
                    json=body,
                    headers={"X-API-Key": args.key, "Idempotency-Key": str(uuid.uuid4())},
                )
                stats[str(r.status_code)] += 1
            except httpx.HTTPError as exc:
                stats[type(exc).__name__] += 1
            await asyncio.sleep(1 / args.rate)
    print(dict(stats))


if __name__ == "__main__":
    asyncio.run(main())
