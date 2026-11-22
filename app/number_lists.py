"""Expand connote lists and ranges written in short form (D-034).

Customers write several connotes at once: `7335842/843/844`, `7336101-103`,
`7336101-7336103`, `614024-610423`. A connote is all digits, at least 6 long;
a shorter number after "/" or "-" is the end of the previous one with its
first digits left out. The rules, agreed with the product owner:

- `/` lists numbers; a short one completes the previous full number.
- `-` with a short end of 3 or more digits is a range: `7336101-103` is
  7336101 to 7336103. A 1 or 2 digit end is part of one connote
  (`4653685-1`, `4653685-01`) and is left as written (product owner).
- `-` between two full numbers is a range only when the second is higher and
  the range holds at most `MAX_RANGE` numbers; otherwise they are two numbers
  (`614024-610423`, `7336101-7336150`, an invoice and a connote as in
  `20260407 - 80655502`), each looked up in Power BI (product owner).
- A short-end range that runs backwards or holds more than `MAX_RANGE`
  numbers is not guessed: it is reported, and the email goes to the team.
- Only lists whose full numbers include one the extractor already found are
  read, so "2/3 pallets" or a date is left alone; numbers with letters
  (`XY62302-03`, `MN675418-3`) are never expanded.
"""

from __future__ import annotations

import re

MAX_RANGE = 20  # product owner: a longer range is more likely a typing error than a request

# A full connote (6+ digits, not inside a word or a code) followed by "/" or
# "-" parts, spaces allowed around the signs.
_LIST = re.compile(r"(?<![A-Za-z0-9/\-])(\d{6,})((?:\s*[/\-]\s*\d+)+)(?![A-Za-z0-9/\-])")
_PART = re.compile(r"\s*([/\-])\s*(\d+)")


def _numbers_of(head: str, tail: str) -> tuple[list[str] | None, list[str], str | None]:
    """(numbers, short parts as written, problem) for one written list; numbers None if it is not a list."""
    numbers, shorts = [head], []
    base = head
    for sign, part in _PART.findall(tail):
        if len(part) >= 6:
            if sign == "-" and int(part) > int(base) and int(part) - int(base) + 1 <= MAX_RANGE:
                numbers += [str(value).zfill(len(base)) for value in range(int(base) + 1, int(part) + 1)]
            else:
                numbers.append(part)
            base = part
            continue
        if sign == "-" and len(part) <= 2:
            return None, [], None  # "4653685-1", "4653685-01": one connote as written (product owner)
        shorts.append(part)  # under 6 digits, so always shorter than the 6+ digit base
        full = base[: len(base) - len(part)] + part
        if sign == "/":
            numbers.append(full)
        elif int(full) < int(base) or int(full) - int(base) + 1 > MAX_RANGE:
            return [], [], f"{head}{tail}".strip()
        else:
            numbers += [str(value).zfill(len(base)) for value in range(int(base) + 1, int(full) + 1)]
        base = full
    return numbers, shorts, None


def expand(text: str, found: list[str]) -> tuple[list[str], list[str]]:
    """`found` with every short-form list in `text` written out, in order; and the lists that could not be read.

    A list replaces the first of its numbers in `found`; a short part that the
    extractor returned on its own ("843") is dropped, as it is not a number.
    """
    # The CRF can return a list as one piece ("7336101-103"): its first number stands for it.
    head_of = lambda number: re.split(r"[/\-]", number.upper())[0] if re.fullmatch(r"\d+([/\-]\d+)+", number) else number.upper()
    found_set = {head_of(number) for number in found}
    expansions: dict[str, list[str]] = {}
    shorts: set[str] = set()
    problems: list[str] = []
    for match in _LIST.finditer(text or ""):
        numbers, short_parts, problem = _numbers_of(match.group(1), match.group(2))
        if numbers is None and problem is None:
            continue  # a connote with a part number, kept as the extractor read it
        fulls = [match.group(1)] + [part for _, part in _PART.findall(match.group(2)) if len(part) >= 6]
        if not any(number in found_set for number in fulls):
            continue  # not a list of requested numbers
        if problem:
            problems.append(problem)
            continue
        expansions.setdefault(next(number for number in fulls if number in found_set), numbers)
        shorts.update(short_parts)
    result: list[str] = []
    for number in found:
        if head_of(number) in expansions:
            result += expansions[head_of(number)]
        elif number not in shorts:
            result.append(number)
    return list(dict.fromkeys(result)), problems
