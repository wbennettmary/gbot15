"""OpenRouter/free name generation for Inbox Intelligence.

The model only proposes candidates. The route validates the shape and the
database reservation table is the source of truth for uniqueness.
"""

import json
import re
import unicodedata

import requests


OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_FREE_MODEL = 'openrouter/free'

_MAX_NAME_PART_LENGTH = 120
_MAX_CONTEXT_NAMES = 180


def normalize_name_key(value):
    """Return a stable, case/diacritic-insensitive key for uniqueness checks."""
    text = unicodedata.normalize('NFKD', str(value or ''))
    text = ''.join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    # Drop punctuation and spacing, so O'Connor, OConnor, and O-Connor reserve
    # the same identity even when a provider formats the surname differently.
    return ''.join(char for char in text if char.isalnum())


def _clean_name_part(value):
    value = unicodedata.normalize('NFKC', str(value or ''))
    value = ''.join(char for char in value if not unicodedata.category(char).startswith('C'))
    value = re.sub(r'\s+', ' ', value).strip()
    if not value or len(value) > _MAX_NAME_PART_LENGTH:
        return ''
    if any(char.isdigit() for char in value) or '@' in value:
        return ''
    return value


def _extract_json_payload(content):
    """Parse JSON from a provider response, including fenced JSON output."""
    text = str(content or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass

    starts = [index for index in (text.find('{'), text.find('[')) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    ends = [index for index in (text.rfind('}'), text.rfind(']')) if index >= start]
    if not ends:
        return None
    try:
        return json.loads(text[start:max(ends) + 1])
    except (TypeError, ValueError):
        pass

    # Free providers occasionally truncate a long JSON array/object at the
    # token limit. Recover every complete name object that did arrive instead
    # of discarding the whole batch because the final comma/brace is missing.
    decoder = json.JSONDecoder()
    recovered = []
    for match in re.finditer(r'\{', text):
        try:
            item, _end = decoder.raw_decode(text, match.start())
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and (
            item.get('given_name') or item.get('first_name')
        ) and (
            item.get('family_name') or item.get('last_name')
        ):
            recovered.append(item)
    return {'names': recovered} if recovered else None


def _response_content(payload):
    choices = payload.get('choices') if isinstance(payload, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return ''
    message = choices[0].get('message') or {}
    content = message.get('content', '')
    if isinstance(content, list):
        return ''.join(
            str(item.get('text') or '')
            for item in content
            if isinstance(item, dict)
        )
    return str(content or '')


def normalize_candidates(raw_payload, limit=180):
    """Validate provider output and derive the complete display name locally."""
    if isinstance(raw_payload, dict):
        raw_names = raw_payload.get('names') or raw_payload.get('results') or []
    elif isinstance(raw_payload, list):
        raw_names = raw_payload
    else:
        raw_names = []

    normalized = []
    seen = set()
    for item in raw_names[:limit]:
        if isinstance(item, str):
            pieces = item.strip().split()
            if len(pieces) < 2:
                continue
            given_name = _clean_name_part(pieces[0])
            family_name = _clean_name_part(' '.join(pieces[1:]))
            region = ''
            country = ''
        elif isinstance(item, dict):
            given_name = _clean_name_part(item.get('given_name') or item.get('first_name'))
            family_name = _clean_name_part(item.get('family_name') or item.get('last_name'))
            region = _clean_name_part(item.get('region'))
            country = _clean_name_part(item.get('country'))
        else:
            continue

        if not given_name or not family_name:
            continue
        full_name = f'{given_name} {family_name}'
        key = normalize_name_key(full_name)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append({
            'full_name': full_name,
            'given_name': given_name,
            'family_name': family_name,
            'region': region[:80],
            'country': country[:120],
            'normalized_name': key,
        })
    return normalized


def request_candidates(
    api_key,
    count,
    *,
    region='worldwide',
    name_style='balanced',
    guidance='',
    excluded_names=None,
    referer='GBot',
    timeout=30,
):
    """Ask OpenRouter/free for candidate names and return validated candidates."""
    count = max(1, min(int(count), 180))
    excluded = [str(value).strip() for value in (excluded_names or []) if str(value).strip()]
    excluded = excluded[-_MAX_CONTEXT_NAMES:]
    excluded_block = '\n'.join(f'- {value}' for value in excluded) or '- none'
    prompt = f"""Generate {count} complete, plausible human names for a worldwide identity library.

Geographic scope: {region}.
Name style: {name_style}.
Additional guidance: {guidance or 'Use a balanced mix of real naming traditions and scripts appropriate to the requested scope.'}

Rules:
- Use a given name and a family name for every entry.
- Favor genuine, culturally coherent names from different countries and regions.
- Do not use celebrities, fictional characters, public figures, placeholders, initials, numbers, emails, or invented keyboard strings.
- Do not repeat a complete name or a close spelling variant within this response.
- Return JSON only in this exact shape: {{"names":[{{"given_name":"...","family_name":"...","region":"...","country":"..."}}]}}.
- The application will enforce final uniqueness, so do not explain your work.

Names already reserved or rejected in this session; do not return them:
{excluded_block}
"""
    response = requests.post(
        OPENROUTER_URL,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': referer or 'GBot',
            'X-Title': 'GBot Inbox Intelligence - Worldwide Name Generator',
        },
        json={
            'model': OPENROUTER_FREE_MODEL,
            'temperature': 0.85,
            # A batch is intentionally bounded by the worker, but this leaves
            # enough room for valid JSON when a free provider returns metadata.
            'max_tokens': 5000,
            'messages': [
                {
                    'role': 'system',
                    'content': 'You are a structured worldwide name data generator. Output only valid JSON.',
                },
                {'role': 'user', 'content': prompt},
            ],
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f'OpenRouter returned HTTP {response.status_code}.')
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError('OpenRouter returned an invalid response.') from exc
    parsed = _extract_json_payload(_response_content(payload))
    candidates = normalize_candidates(parsed, limit=count)
    if not candidates:
        raise RuntimeError('OpenRouter returned no valid complete names. Try again with a broader scope.')
    return candidates
