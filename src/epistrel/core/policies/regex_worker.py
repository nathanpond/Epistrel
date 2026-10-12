"""The regex search a policy's ``regex`` rule runs in a child process (``evaluate``).

``evaluate`` starts this file as a script (``python -I regex_worker.py``), so it imports only the
standard library and nothing of ``epistrel``. The protocol is one JSON line per request
(``{"p": [patterns], "t": text}``) on stdin and one line per answer (``1`` or ``0``) on stdout,
after a first ``ready`` line. The worker exits when stdin closes; a search that misses its
deadline is killed by the parent (#290).
"""

from __future__ import annotations


def search_any(patterns: list[str] | tuple[str, ...], text: str) -> bool:
    """Whether any pattern matches anywhere in ``text``, ignoring case."""
    import re

    return any(re.search(pattern, text, re.IGNORECASE) is not None for pattern in patterns)


def main() -> None:  # pragma: no cover - runs in the child process
    import json
    import sys

    out = sys.stdout
    out.write("ready\n")
    out.flush()
    for line in sys.stdin:
        request = json.loads(line)
        out.write("1\n" if search_any(request["p"], request["t"]) else "0\n")
        out.flush()


if __name__ == "__main__":  # pragma: no cover
    main()
