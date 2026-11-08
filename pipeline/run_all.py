"""Runs the offline pipeline in order: collect -> load -> chunk -> embed -> store -> export.

Each stage runs as its own `python -m pipeline.<stage>` process and the run
stops at the first stage that fails. Extra arguments are not forwarded.

Usage:
    python -m pipeline.run_all [--from STAGE] [--to STAGE]

Examples:
    python -m pipeline.run_all                 # full rebuild (fetches the web pages again)
    python -m pipeline.run_all --from chunk    # rebuild from the already-loaded documents
"""

import argparse
import subprocess
import sys
import time

from app.config import PROJECT_ROOT

STAGES = ["collect", "load", "chunk", "embed", "store", "export"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", choices=STAGES, default=STAGES[0])
    parser.add_argument("--to", dest="end", choices=STAGES, default=STAGES[-1])
    args = parser.parse_args()
    stages = STAGES[STAGES.index(args.start):STAGES.index(args.end) + 1]
    if not stages:
        parser.error("--from must come before --to")

    for stage in stages:
        print(f"\n=== {stage} ===", flush=True)
        t0 = time.monotonic()
        code = subprocess.call([sys.executable, "-m", f"pipeline.{stage}"], cwd=PROJECT_ROOT)
        if code != 0:
            print(f"\nPipeline stopped: {stage} failed (exit {code}).", file=sys.stderr)
            sys.exit(code)
        print(f"=== {stage} done in {time.monotonic() - t0:.1f}s ===", flush=True)
    print(f"\nPipeline finished: {' -> '.join(stages)}")


if __name__ == "__main__":
    main()
