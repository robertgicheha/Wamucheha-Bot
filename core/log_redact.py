"""
Keeps secrets out of logs. Telegram and Discord put the credential inside the URL, and
`requests` repeats the URL in its error messages, so any network failure would write the
token to disk and to the admin console's log viewer.
"""
import logging
import re

_PATTERNS = (
    (re.compile(r"(?<![A-Za-z0-9])(bot)\d{5,}:[A-Za-z0-9_-]{20,}"), r"\1<redacted>"),
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/|/api/webhooks/\d+/)[A-Za-z0-9_-]+"), r"\1<redacted>"),
)


def redact(text) -> str:
    out = str(text)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFilter(logging.Filter):
    """Cleans a record's finished message, so secrets passed as arguments are caught too."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True
