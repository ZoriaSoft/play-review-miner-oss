"""Mask secrets that provider error bodies may echo back.

Provider errors are shown in the (publicly reachable) panel and in logs, so
anything looking like a credential must be replaced with ``***`` first:
structured ``api_key``-style values, ``Bearer ...`` tokens and ``key=...``
parameters. Only synthetic keys are ever used in tests.
"""

from __future__ import annotations

import re

# "api_key": "value" / 'x-goog-api-key': 'value' (JSON-ish, quoted value)
_STRUCTURED = re.compile(
    r"""(?ix)
    (?P<name>"(?:api[_-]?key|x-goog-api-key|x-api-key|authorization)"|'(?:api[_-]?key|x-goog-api-key|x-api-key|authorization)')
    (?P<sep>\s*:\s*)
    (?P<value>"[^"]*"|'[^']*')
    """)
# api_key=value, api-key: value, x-goog-api-key: value (unquoted value)
_NAMED = re.compile(
    r"""(?ix)
    \b(?P<name>api[_-]?key|x-goog-api-key|x-api-key)\b
    (?P<sep>\s*[:=]\s*)
    (?P<value>"[^"]*"|'[^']*'|[^\s,;\"'}\]]+)
    """)
_ESCAPED_STRUCTURED = re.compile(
    r"""(?ix)
    (?P<name>\\"(?:api[_-]?key|x-goog-api-key|x-api-key|authorization)\\")
    (?P<sep>\s*:\s*)
    (?P<value>\\"[^"]*\\")
    """)
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_KEY_PARAM = re.compile(r"(?i)\bkey\s*=\s*[^\s&;,\"']+")
_AUTH_HEADER = re.compile(r"(?i)\bAuthorization\s*:\s*(?!Bearer\b)\S+")


def redact(text: str | None) -> str | None:
    """Return `text` with credential-like values replaced by ``***``."""
    if not text:
        return text
    out = _ESCAPED_STRUCTURED.sub(lambda m: f"{m.group('name')}{m.group('sep')}\\\"***\\\"", text)
    out = _STRUCTURED.sub(lambda m: f"{m.group('name')}{m.group('sep')}\"***\"", out)
    out = _NAMED.sub(lambda m: f"{m.group('name')}{m.group('sep')}***", out)
    out = _AUTH_HEADER.sub("Authorization: ***", out)
    out = _BEARER.sub("Bearer ***", out)
    out = _KEY_PARAM.sub("key=***", out)
    return out
