"""Runtime extraction of connote and reference numbers: CRF first, regex as fallback."""

from __future__ import annotations

from . import entity_extractor, number_lists


def extract_numbers(
    model,
    subject: str,
    body: str,
    confidence_threshold: float = 0.5,
    full_body: str = "",
    problems: list[str] | None = None,
) -> list[str]:
    """Connote and reference numbers requested by an email, in order, unique.

    1. With the CRF chosen (`model`), the numbers it is at least
       `confidence_threshold` sure of in the newest message (D-026).
    2. Otherwise, or when it finds none, the regex extractor on the newest
       message, so a request is never left without a number the regex sees.
    3. If still none, the request may be a forward ("please send the POD for
       these" above the forwarded email that lists them): regex reads the
       whole email, `full_body` if given, else `body`, including text after
       the sign-off and quoted history (D-017).

    Whichever finds them, lists written in short form in that same text
    (`7335842/843/844`, `7336101-103`) are written out in full (D-034). A list
    that cannot be read safely is added to `problems`, if given, for the
    caller to hold the email.
    """
    from . import number_crf

    def written_out(numbers: list[str], text: str) -> list[str]:
        expanded, unclear = number_lists.expand(f"{subject}\n{text}", numbers)
        if problems is not None:
            problems.extend(unclear)
        return expanded

    latest_body = entity_extractor.extract_latest_email_body(body)
    if model is not None:
        numbers = number_crf.extract(model, subject, latest_body, confidence_threshold)
        if numbers:
            return written_out(numbers, latest_body)
    numbers = entity_extractor.extract_email_connotes(subject, latest_body)
    if numbers:
        return written_out(numbers, latest_body)
    whole = full_body or body
    return written_out(entity_extractor.extract_email_connotes(subject, whole, stop_at_signoff=False), whole)
