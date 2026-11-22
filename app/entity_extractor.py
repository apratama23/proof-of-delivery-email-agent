"""Extract possible connote or reference numbers from email text."""

import re

from . import config


_CANDIDATE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{5,15}(?![A-Za-z0-9])")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9-]+")
_SIGNOFF_PATTERN = re.compile(r"\bKIND\s+REGARDS\b|\bREGARDS\b", re.IGNORECASE)
_EXTERNAL_WARNING_PATTERN = re.compile(
    r"CAUTION:\s*This email originated from outside of the organisation\.\s*"
    r"Do not click links or open attachments unless you recognize the sender "
    r"and know the content is safe\.",
    re.IGNORECASE | re.DOTALL,
)
_SAFE_LINK_PATTERN = re.compile(
    r".*safelinks\.protection\.outlook\.com.*(?:\r?\n|$)",
    re.IGNORECASE,
)
_TRAIL_MARKER_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:-{3,}\s*Original Message\s*-{3,}|"
    r"From:\s*.+\n(?:Sent|Date):\s*.+\n(?:To):\s*.+(?:\nSubject:\s*.+)?)",
    re.IGNORECASE | re.MULTILINE,
)


def extract_candidates(text: str) -> list[str]:
    """Return unique alphanumeric tokens that may be connote numbers."""
    if not text:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for match in _CANDIDATE_PATTERN.finditer(text):
        candidate = match.group(0).upper()
        if not any(character.isalpha() for character in candidate):
            continue
        if not any(character.isdigit() for character in candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def extract_cargo_keywords(text: str) -> list[str]:
    """Return cargo keywords when this optional feature is needed later."""
    return []


def extract_latest_email_body(body: str) -> str:
    """Remove quoted reply/forward content and keep the newest message body."""
    if not body:
        return ""
    latest = _TRAIL_MARKER_PATTERN.split(body, maxsplit=1)[0]
    latest = _EXTERNAL_WARNING_PATTERN.sub("", latest)
    latest = _SAFE_LINK_PATTERN.sub("", latest)
    latest = re.sub(r"\n\s*\n(?:\s*\n)+", "\n\n", latest)
    return latest.strip()


def extract_email_connotes(
    subject: str, body: str, stop_at_signoff: bool = True
) -> list[str]:
    """Extract connote formats used by the POD workflow from an email.

    By default only the text before the first "Regards" is read, so numbers in
    signatures are not picked up. `stop_at_signoff=False` reads everything,
    for the forwarded-request fallback (D-017).
    """
    body_before_signoff = (
        _SIGNOFF_PATTERN.split(body or "", maxsplit=1)[0]
        if stop_at_signoff
        else body or ""
    )
    search_text = f"{subject or ''},{body_before_signoff}"
    candidates: list[str] = []
    seen: set[str] = set()

    for raw_token in _TOKEN_PATTERN.findall(search_text):
        token = raw_token.strip().rstrip(".").upper()
        if re.fullmatch(r"\d+(-\d{3,})+", token):
            # "7336101-103" is a range written short: its first number counts;
            # number_lists writes the rest out (D-034). "4653685-01" is one
            # connote and stays as it is.
            token = token.split("-", 1)[0]
        if _is_numeric_connote(token) or _is_prefixed_connote(token):
            if token not in seen:
                seen.add(token)
                candidates.append(token)
    return candidates


def looks_like_number(token: str) -> bool:
    """Whether a token has the form of a connote or reference number, by the regex rule."""
    token = token.strip().upper()
    return _is_numeric_connote(token) or _is_prefixed_connote(token)


def _is_numeric_connote(token: str) -> bool:
    return (
        token.isdigit()
        and 6 <= len(token) <= 10
        and not token.startswith("04")
    )


def _is_prefixed_connote(token: str) -> bool:
    for prefix in config.connote_prefixes():  # set in .env (D-060)
        if token.startswith(prefix) and len(token) > len(prefix):
            next_character = token[len(prefix)]
            return next_character == "-" or next_character.isdigit()
    return False