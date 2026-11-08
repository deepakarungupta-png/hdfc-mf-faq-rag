"""Ask a question without the HTTP layer: python scripts/ask.py "question" [--log]"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.service import ask  # noqa: E402


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--log"]
    if len(args) != 1 or not args[0].strip():
        sys.exit('usage: python scripts/ask.py "question" [--log]')
    resp, record = ask(args[0].strip())
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    if "--log" in sys.argv:
        print(json.dumps(record), file=sys.stderr)


if __name__ == "__main__":
    main()
