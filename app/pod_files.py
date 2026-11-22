"""Find POD scans on the network share by file name (D-003)."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from urllib.parse import unquote, urlsplit

_REMOVED_CHARACTERS = re.compile(r"[\s\-,.]+")


def clean(value: str) -> str:
    """The one cleaning rule for requested numbers and file names alike.

    Removes spaces, "-", "," and "." and upper-cases, so "AB-10234" matches
    "ab10234_x.pdf". File names are cleaned without their ".pdf" extension.
    """
    return _REMOVED_CHARACTERS.sub("", value).upper()


# A scan's name is numbers and words apart, "1234567_80655501 - Signed (2)"
# (D-042): "signed" or "unsigned" with its copy number is left out first.
_SIGNED_WORD = re.compile(r"\b(?:un)?signed\b\s*(?:\(\s*\d+\s*\))?", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s_\-.,()]+")


def name_parts(stem: str) -> list[str]:
    """The parts of a file name, in order and upper-cased, without Signed or Unsigned and its copy number."""
    return [part for part in _SEPARATORS.split(_SIGNED_WORD.sub(" ", stem).upper()) if part]


def name_tokens(stem: str) -> frozenset[str]:
    """The numbers and codes in a scan's file name, each whole (D-042).

    "1234567_80655501 - Signed (2)" gives 1234567 and 80655501. A prefix or a
    part number written apart also counts joined ("AB-10234" also AB10234,
    "4653685-01" also 465368501), as a requested number is written joined.
    A number inside another ("7398507" in the pallet number PLT73985078J)
    is never one of them.
    """
    parts = name_parts(stem)
    joined = {
        first + second for first, second in zip(parts, parts[1:])
        if (first.isalpha() and second.isdigit()) or (first.isdigit() and second.isdigit() and len(second) <= 2)
    }
    return frozenset(parts) | joined


def _signed(path: Path) -> bool:
    return "signed" in path.stem.lower()  # "unsigned" scans are not indexed at all


@dataclass(frozen=True)
class FileIndex:
    """Every PDF under the scan folders, with its name as written (without ".pdf")."""

    roots: tuple[Path, ...]
    entries: tuple[tuple[str, Path], ...]
    seconds: float

    @cached_property
    def tokens(self) -> tuple[frozenset[str], ...]:
        """`name_tokens` of every entry, worked out once per index."""
        return tuple(name_tokens(stem) for stem, _ in self.entries)

    def matches(self, number: str) -> list[Path]:
        """The files for a number, sorted (D-042).

        A file counts when one whole part of its name is the number: exactly
        as written first, else with more or fewer leading zeros (`89855504`,
        `0089855504`). Among those, a "signed" scan wins over one without the
        word; several signed scans are for the team to choose.
        """
        wanted = clean(number)
        if not wanted:
            return []
        core = wanted.lstrip("0") or wanted
        # shortcut: linear scan of every name per number; about 20,000 names
        # and a handful of numbers per email is instant. Build a token index
        # if the share grows by orders of magnitude.
        files = [path for tokens, (_, path) in zip(self.tokens, self.entries) if wanted in tokens] or [
            path for tokens, (_, path) in zip(self.tokens, self.entries)
            if core in {token.lstrip("0") or token for token in tokens}
        ]
        return sorted([path for path in files if _signed(path)] or files)

    @cached_property
    def by_path(self) -> dict[str, Path]:
        """Every indexed file by its resolved, normalised path, built once (`indexed_path`).

        Only the roots are resolved, not each file: resolving every file on a
        network share took minutes.
        """
        roots = [(Path(root), Path(root).resolve()) for root in self.roots]
        lookup: dict[str, Path] = {}
        for _, path in self.entries:
            for root, resolved_root in roots:
                if Path(path).is_relative_to(root):
                    lookup[os.path.normcase(os.path.normpath(str(resolved_root / Path(path).relative_to(root))))] = path
                    break
        return lookup


def is_inside(path: Path, roots: list[Path]) -> bool:
    """Whether `path` lies under one of the scan folders (attachment safety)."""
    resolved = Path(path).resolve()
    return any(resolved.is_relative_to(Path(root).resolve()) for root in roots)


def path_from_uri(value: str) -> Path | None:
    """Convert a local path or file URI to a filesystem path."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.scheme.casefold() != "file":
            return None
        if parsed.scheme.casefold() == "file":
            uri_path = unquote(parsed.path)
            if parsed.netloc:
                candidate = Path("\\\\" + parsed.netloc + uri_path.replace("/", "\\"))
            else:
                if os.name == "nt" and re.match(r"^/[A-Za-z]:", uri_path):
                    uri_path = uri_path[1:]
                candidate = Path(uri_path)
        else:
            candidate = Path(value)
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def indexed_path(index: FileIndex, value: str) -> Path | None:
    """Return an exact indexed PDF from a local path or file URI inside the scan roots."""
    candidate = path_from_uri(value)
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve()
        if not is_inside(resolved, list(index.roots)):
            return None
        return index.by_path.get(os.path.normcase(os.path.normpath(str(resolved))))
    except (OSError, RuntimeError, ValueError):
        return None


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_same_or_child(path: Path, parent: str) -> bool:
    try:
        return os.path.commonpath([_normalized_path(path), parent]) == parent
    except ValueError:
        return False


def _exclusion_rules(
    roots: list[Path], excluded_folders: list[Path] | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized_roots = tuple(Path(root).resolve() for root in roots)
    excluded_paths: list[str] = []
    excluded_names: list[str] = []
    for excluded in excluded_folders or []:
        configured_path = Path(excluded)
        if not configured_path.is_absolute() and len(configured_path.parts) == 1:
            if configured_path.name in {"", ".", ".."}:
                raise ValueError(f"Invalid excluded scan folder name: {excluded}")
            excluded_names.append(configured_path.name.casefold())
            continue
        candidates = (
            [configured_path.resolve()]
            if configured_path.is_absolute()
            else [root / configured_path for root in normalized_roots]
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            if not any(_is_same_or_child(resolved, _normalized_path(root)) for root in normalized_roots):
                raise ValueError(f"Excluded scan folder is outside POD_SCAN_FOLDERS: {excluded}")
            excluded_paths.append(_normalized_path(resolved))
    return tuple(excluded_paths), tuple(excluded_names)


def is_excluded(path: Path, roots: list[Path], excluded_folders: list[Path] | None) -> bool:
    """Whether a path lies in a configured excluded scan folder."""
    resolved = Path(path).resolve()
    excluded_paths, excluded_names = _exclusion_rules(roots, excluded_folders)
    if any(_is_same_or_child(resolved, excluded) for excluded in excluded_paths):
        return True
    normalized_roots = tuple(Path(root).resolve() for root in roots)
    return any(
        parent.name.casefold() in excluded_names
        and any(_is_same_or_child(parent, _normalized_path(root)) for root in normalized_roots)
        for parent in resolved.parents
    )


def build_index(roots: list[Path], excluded_folders: list[Path] | None = None) -> FileIndex:
    """Walk every root folder once and collect its PDFs.

    A folder name excludes matching folders anywhere under all roots.
    Paths containing subfolders are relative to each root; absolute paths are
    also supported.
    A missing or unreadable root raises, so a disconnected share is reported
    instead of making every POD look "not received".
    """
    started = time.monotonic()
    entries: list[tuple[str, Path]] = []
    excluded_paths, excluded_names = _exclusion_rules(roots, excluded_folders)

    for root in roots:
        if not Path(root).is_dir():
            raise FileNotFoundError(f"Scan folder not reachable: {root}")

        def fail(error: OSError) -> None:
            raise error

        for folder, directories, files in os.walk(root, topdown=True, onerror=fail):
            current = Path(folder).resolve()
            current_is_excluded = any(
                _is_same_or_child(current, excluded) for excluded in excluded_paths
            )
            if current_is_excluded:
                directories.clear()
                continue
            directories[:] = [
                name for name in directories
                if name.casefold() not in excluded_names
                and not any(
                    _is_same_or_child((current / name).resolve(), excluded)
                    for excluded in excluded_paths
                )
            ]
            for name in files:
                stem, extension = os.path.splitext(name)
                # An "Unsigned" scan is never sent (product owner, D-042).
                if extension.lower() == ".pdf" and "unsigned" not in stem.lower():
                    entries.append((stem, Path(folder) / name))
    return FileIndex(tuple(Path(root) for root in roots), tuple(entries), time.monotonic() - started)


@dataclass
class SearchResult:
    found: dict[str, Path]
    ambiguous: dict[str, list[Path]]
    missing: list[str]
    # Numbers matched only outside the sender's own folder; they are in
    # `ambiguous`, so the team confirms before such a file is sent.
    outside_folder: list[str] = field(default_factory=list)


def _in_folder(path: Path, roots: tuple[Path, ...], folder: str) -> bool:
    """Whether `path` lies in `folder` (a path relative to a scan folder, e.g. "Customer")."""
    wanted = [part.casefold() for part in Path(folder.replace("\\", "/")).parts]
    for root in roots:
        try:
            parts = Path(path).relative_to(root).parent.parts
        except ValueError:
            continue
        if [part.casefold() for part in parts[: len(wanted)]] == wanted:
            return True
    return False


def search(index: FileIndex, numbers: list[str], folder: str | None = None) -> SearchResult:
    """Sort each number into FOUND (one file), AMBIGUOUS (several) or MISSING.

    With `folder` (the sender's own scan folder under each scan folder,
    `config.sender_folder`), only files in it count; files that match only
    elsewhere are AMBIGUOUS, never sent without the team's choice.
    """
    result = SearchResult(found={}, ambiguous={}, missing=[])
    for number in numbers:
        files = index.matches(number)
        if folder and files:
            inside = [path for path in files if _in_folder(path, index.roots, folder)]
            if not inside:
                result.ambiguous[number] = files
                result.outside_folder.append(number)
                continue
            files = inside
        if len(files) == 1:
            result.found[number] = files[0]
        elif files:
            result.ambiguous[number] = files
        else:
            result.missing.append(number)
    return result
