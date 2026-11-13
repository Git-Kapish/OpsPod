"""Golden-set eval + release gate. Exit code 1 when the gate fails.

make eval        # dev split, replay cassettes if any are recorded, else the stub LLM
make eval-live   # live providers (throttled by the gateway), records cassettes
--split holdout  # release checks only; never tune against it
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opspod.evals import harness, metrics, report  # noqa: E402
from opspod.llm.factory import CASSETTE_DIR  # noqa: E402


def pick_llm(requested: str, live: bool) -> str:
    if live:
        return "live"
    if requested != "auto":
        return requested
    return "replay" if any(CASSETTE_DIR.glob("*.json")) else "stub"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    ap.add_argument("--llm", choices=["auto", "stub", "replay", "live"], default="auto")
    ap.add_argument("--live", action="store_true", help="shorthand for --llm live")
    ap.add_argument("--stub", choices=list(harness.STUBS), default="good")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--no-report", action="store_true", help="do not rewrite EVALS.md")
    args = ap.parse_args()

    llm = pick_llm(args.llm, args.live)
    cases = harness.load_cases(args.split)
    print(
        f"running {len(cases)} cases, split={args.split}, llm={llm}"
        + (f" (stub={args.stub})" if llm == "stub" else "")
    )
    results, events = await harness.run_eval(cases, llm=llm, stub=args.stub)
    m = metrics.compute(results, events)
    baseline = report.load_baseline(llm)
    fails = metrics.gate(m, baseline)
    regress = metrics.regressions(m, baseline)
    run = report.make_run(m, args.split, llm, args.stub)
    tag = f"{llm}-{args.stub}" if llm == "stub" else llm
    path = report.save_run(run, f"{tag}-{args.split}-{run['meta']['at'][:10]}")
    if not args.no_report:
        report.write_report(run, results, fails, regress)
    if args.update_baseline and not fails:
        report.baseline_path(llm).write_text(
            json.dumps(m, indent=1) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"baseline updated: {report.baseline_path(llm)}")
    print(
        f"correctness={m['correctness']:.1%} "
        f"compliance(safety)={m['policy_compliance_safety']:.1%} "
        f"route={m['route_accuracy']:.1%} tools={m['tool_selection_accuracy']:.1%}"
    )
    print(f"results: {path}")
    for f in fails:
        print(f"GATE FAIL: {f}")
    print("GATE PASS" if not fails else "GATE FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
