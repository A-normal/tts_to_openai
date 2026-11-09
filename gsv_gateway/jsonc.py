"""Small JSONC lexer: comments become whitespace; JSON remains strictly parsed.

No regular-expression stripping: URLs, escaped quotes and comment-like strings
must survive. Duplicate object keys and non-finite numbers are rejected.
"""

import json
from pathlib import Path


def strip_comments(source: str) -> str:
    out = list(source)
    i = 0
    quoted = False
    while i < len(source):
        c = source[i]
        if quoted:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif source.startswith("//", i):
            while i < len(source) and source[i] not in "\r\n":
                out[i] = " "
                i += 1
            continue
        elif source.startswith("/*", i):
            start = i
            out[i : i + 2] = "  "
            i += 2
            while i < len(source) and not source.startswith("*/", i):
                if source[i] not in "\r\n":
                    out[i] = " "
                i += 1
            if i == len(source):
                raise ValueError(f"Unterminated JSONC comment at offset {start}")
            out[i : i + 2] = "  "
            i += 2
            continue
        i += 1
    return "".join(out)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Non-finite JSON number")


def loads(source: str):
    return json.loads(
        strip_comments(source.lstrip("\ufeff")), object_pairs_hook=_pairs, parse_constant=_constant
    )


def load(path: Path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Configuration exceeds 4 MiB")
    return loads(path.read_text(encoding="utf-8-sig"))
