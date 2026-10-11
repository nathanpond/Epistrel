"""The regex search a policy's ``regex`` rule runs in a subinterpreter (``evaluate``).

The interpreter pool runs this function's code without the module's globals, so it imports what
it needs itself. Only ``re`` — a subinterpreter loads it cheaply and safely.
"""


def search_any(patterns: tuple[str, ...], text: str) -> bool:  # pragma: no cover - subinterpreter
    """Whether any pattern matches anywhere in ``text``, ignoring case."""
    import re

    return any(re.search(pattern, text, re.IGNORECASE) is not None for pattern in patterns)
