"""Redaction helpers for AI Placement Optimization (Phase 1).

Everything that leaves GBot toward OpenRouter must pass through
redact_context() first. Secrets are replaced with [redacted] and long
free-text fields are truncated so private mailbox payloads never reach
the provider in full.
"""

import re

REDACTED = '[redacted]'

DEFAULT_TEXT_LIMIT = 4000

_SECRET_KEY_PATTERN = re.compile(
    r'(?i)\b('
    r'api[_-]?key|apikey|secret|client[_-]?secret|secret[_-]?key|'
    r'password|app[-_]?password|passwd|pwd|'
    r'authorization|auth[_-]?header|cookie|set[-_]?cookie|session[_-]?id|'
    r'access[_-]?token|refresh[_-]?token|id[_-]?token|bearer[_-]?token|token|'
    r'private[_-]?key|service[_-]?account|credentials'
    r')\b\s*[:=]\s*("[^"]*"|\'[^\']*\'|[^\s,;&]+)'
)

_BEARER_PATTERN = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}')

_PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----.{0,4000}?-----END [A-Z ]*PRIVATE KEY-----',
    re.DOTALL,
)

_AWS_ACCESS_KEY_PATTERN = re.compile(r'\bAKIA[0-9A-Z]{16}\b')

_GOOGLE_API_KEY_PATTERN = re.compile(r'\bAIza[0-9A-Za-z_\-]{35}\b')

_OPENAI_STYLE_KEY_PATTERN = re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')

_HIDDEN_BLOCK_PATTERN = re.compile(
    r'<(script|style)\b[^>]*>.*?</\1\s*>|<!--.*?-->|'
    r'<div[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden)[^>]*>.*?</div>',
    re.IGNORECASE | re.DOTALL,
)


def redact_text(value):
    """Return value with known secret shapes replaced by [redacted]."""
    if not isinstance(value, str) or not value:
        return value if isinstance(value, str) else ''
    redacted = _PRIVATE_KEY_BLOCK_PATTERN.sub(REDACTED, value)
    redacted = _BEARER_PATTERN.sub(f'Bearer {REDACTED}', redacted)
    redacted = _AWS_ACCESS_KEY_PATTERN.sub(REDACTED, redacted)
    redacted = _GOOGLE_API_KEY_PATTERN.sub(REDACTED, redacted)
    redacted = _OPENAI_STYLE_KEY_PATTERN.sub(REDACTED, redacted)
    redacted = _SECRET_KEY_PATTERN.sub(lambda m: f'{m.group(1)}: {REDACTED}', redacted)
    return redacted


def truncate_text(value, max_length=DEFAULT_TEXT_LIMIT):
    """Truncate a string to max_length characters."""
    if not isinstance(value, str):
        return ''
    if len(value) <= max_length:
        return value
    return value[:max_length]


def strip_hidden_content(html_value):
    """Strip scripts, styles, comments, and hidden tracking blocks from HTML."""
    if not isinstance(html_value, str) or not html_value:
        return html_value or ''
    return _HIDDEN_BLOCK_PATTERN.sub('', html_value)


def is_sensitive_key(key_name):
    """True when a dict key names a secret-like field."""
    if not isinstance(key_name, str):
        return False
    normalized = key_name.strip().lower().replace('-', '_').replace(' ', '_')
    sensitive_fragments = (
        'password', 'app_password', 'passwd', 'secret', 'api_key', 'apikey',
        'token', 'cookie', 'authorization', 'auth_header', 'credential',
        'private_key', 'service_account',
    )
    return any(fragment in normalized for fragment in sensitive_fragments)


def redact_context(payload, max_text_length=DEFAULT_TEXT_LIMIT):
    """Deep-walk a JSON-like payload, redacting secrets and truncating text.

    Returns a new structure; the input is never mutated.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        result = {}
        for key, value in payload.items():
            if is_sensitive_key(key):
                result[key] = REDACTED
            else:
                result[key] = redact_context(value, max_text_length)
        return result
    if isinstance(payload, (list, tuple)):
        return [redact_context(item, max_text_length) for item in payload]
    if isinstance(payload, str):
        return truncate_text(redact_text(payload), max_text_length)
    if isinstance(payload, (int, float, bool)):
        return payload
    return truncate_text(str(payload), max_text_length)
