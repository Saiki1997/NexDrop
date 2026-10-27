"""CI only: turn each failed test in a unittest log into a GitHub annotation, so a failure can be
read from the check run without opening the full log.

    python tests/ci_annotate.py <unittest output file>
"""

import re
import sys
from pathlib import Path


def blocks(text: str) -> list[tuple[str, str]]:
    out = []
    for part in re.split(r"^={50,}$", text, flags=re.M)[1:]:
        head, _, body = part.strip().partition("\n")
        if head.startswith(("FAIL:", "ERROR:")):
            body = body.split("\n" + "-" * 70 + "\nRan ", 1)[0]
            out.append((head, body.strip("-\n ")))
    return out


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main() -> int:
    text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    for head, body in blocks(text)[:10]:
        print(f"::error title={escape(head[:200]).replace(',', ' ').replace('::', ' ')}::{escape(body[-3500:])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
