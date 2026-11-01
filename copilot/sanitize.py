"""Scrubbing and bounding the one input this service does not control.

The error log is pasted by a human from a production system. Two things follow.

First, it routinely contains secrets. A Postgres log line carries a connection
string, a JVM stack trace carries whatever was in the environment, a curl trace
carries the Authorization header. That text currently reaches three places that
are not ours: the embedding API, the judge, and — this is the one that matters —
DuckDuckGo, because the judge writes `search_query` after reading the log and
`research.py` sends it straight out. A pasted credential can leave the system as
a search engine query. Redacting at ingress closes that at the source: the judge
cannot emit a secret it was never shown.

Second, it is attacker-influenceable. A stack trace contains strings an attacker
chose, and those strings land in a prompt. The prompts here label their sections
with bare uppercase headers, which pasted content can forge — see `fence`.

Redaction is deliberately conservative. Over-redacting a runbook's own subject
matter would be worse than useless: `password_encryption = md5` is a Postgres
setting the staleness audit exists to catch, not a credential, and the patterns
below are written so it survives untouched.
"""

import re

# Hard cap on the log. Nothing legitimate needs more, a bigger paste is either a
# mistake or an attempt to push the system instruction out of attention, and the
# embedding API rejects very long inputs anyway.
MAX_LOG_CHARS = 20_000

REDACTED = "[REDACTED]"

# Ordered most-specific first: a JWT inside an Authorization header should be
# caught by the header rule and reported as one secret, not two.
PATTERNS: list[tuple[str, re.Pattern]] = [
    # PEM blocks, including the body, which is the whole point.
    (
        "private_key",
        re.compile(
            r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Authorization: Bearer <token> / Basic <blob>
    (
        "auth_header",
        re.compile(
            r"(authorization\s*[:=]\s*)(bearer|basic|token)\s+\S+", re.IGNORECASE
        ),
    ),
    # Credentials inside a connection URI: scheme://user:secret@host
    (
        "uri_password",
        re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://[^\s:/@]+:)(?P<secret>[^\s@/]+)(?=@)"),
    ),
    # Provider-shaped keys, which are recognisable on sight and worth naming.
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")),
    # key=value forms. The word boundary before the keyword is what keeps
    # `password_encryption = md5` intact: there, `password` is followed by `_`,
    # so the keyword never completes. Quoted and bare values both handled.
    (
        "credential_assignment",
        re.compile(
            r"\b(?P<key>password|passwd|pgpassword|secret|api[_-]?key|access[_-]?token"
            r"|auth[_-]?token|client[_-]?secret|private[_-]?key)"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?P<value>\"[^\"]+\"|'[^']+'|\S+)",
            re.IGNORECASE,
        ),
    ),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Replace credential-shaped substrings. Returns (clean text, kinds found).

    The kinds are for logging and for the `retrieved` event, so an operator can
    see that redaction happened without the redacted value being echoed back.
    """
    found: list[str] = []

    def mark(kind: str) -> None:
        if kind not in found:
            found.append(kind)

    for kind, pattern in PATTERNS:
        def replace(match: re.Match, kind: str = kind) -> str:
            mark(kind)
            groups = match.groupdict()
            # Keep the part that identifies WHAT was redacted. An engineer reading
            # the answer still needs to know a password was in the log; they do
            # not need the password.
            if "key" in groups and groups.get("key") is not None:
                return f"{groups['key']}{groups['sep']}{REDACTED}"
            if "scheme" in groups and groups.get("scheme") is not None:
                return f"{groups['scheme']}{REDACTED}"
            if match.lastindex and match.group(1):
                return f"{match.group(1)}{REDACTED}"
            return REDACTED

        text = pattern.sub(replace, text)

    return text, found


def cap(text: str, limit: int = MAX_LOG_CHARS) -> str:
    """Bound the input, keeping the head.

    The head, not the tail: the first lines of a log carry the error that was
    being reported, while the tail of an oversized paste is where appended
    instructions would sit.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated at {limit} characters]"


def fence(label: str, content: str, nonce: str) -> str:
    """Wrap untrusted content in markers it cannot forge.

    The prompts used to delimit sections with bare uppercase labels — `ERROR LOG:`
    followed by the content, terminated only by the next such label. Pasted text
    containing a line like `RUNBOOK EXCERPT (source: x):` ended the log section
    early and opened one the model read as trusted.

    The nonce is per-request and unpredictable, so content cannot close its own
    fence. Any stray copy of the marker inside the content is neutralised rather
    than trusted.
    """
    open_tag, close_tag = f"<{label} {nonce}>", f"</{label} {nonce}>"
    content = content.replace(open_tag, "").replace(close_tag, "")
    return f"{open_tag}\n{content}\n{close_tag}"


def clean_log(text: str) -> tuple[str, list[str]]:
    """Everything the error log goes through before it reaches anything else."""
    redacted, found = redact(cap(text))
    return redacted, found
