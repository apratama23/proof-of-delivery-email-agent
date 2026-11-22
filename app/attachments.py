"""Numbers from an email's attachments, for a POD request whose text has none (product owner, D-044).

The sources, in the product owner's order: the names of PDF attachments, the
connote and reference columns of spreadsheets (D-046), the text in attached
pictures (Windows' own OCR, on this PC), then the text in the PDFs (their
text, or their first pages read by OCR when they are scans). The agent
tries them one by one: the next only when every number of the one before is
neither on the share nor in Power BI.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import tempfile
from pathlib import Path

from . import entity_extractor, number_crf, number_lists, outlook_reader, pod_files

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
MIN_IMAGE_BYTES = 20_000  # smaller pictures are logos and icons in signatures
MAX_IMAGES = 5
MAX_PDFS = 5
MAX_PDF_PAGES = 3
MAX_FILE_BYTES = 10_000_000
SPREADSHEET_TYPES = {".xlsx", ".xlsm", ".xls", ".csv"}
MAX_SPREADSHEETS = 3
MAX_SHEETS = 3
HEADER_ROWS = 10  # the header row is looked for in a sheet's first rows
# A connote or reference column's header (product owner): Connote, Consignment, Con Note, DO Number, Reference, Ref, Order No.
NUMBER_HEADER = re.compile(
    r"\b(con ?notes?|consignments?|do ?(no|num|number)|donumber|references?|refs?|order ?(no|num|number))\b"
)
# A number read from a picture or a document must be surer than one in the text (D-044).
DOCUMENT_THRESHOLD = 0.9


def read(entry_id: str, store_id: str, number_model) -> dict:
    """{"sources": [{"source", "numbers"}...] in order, only those with numbers; "not_read": [file: why]}.

    The attachments are saved in a temporary folder that is deleted at once.
    """
    with tempfile.TemporaryDirectory() as folder:
        files = outlook_reader.save_attachments(entry_id, store_id, Path(folder))
        return numbers_from(files, number_model)


def numbers_from(files: list[dict], number_model) -> dict:
    """The numbers of each source in `files` ({"name", "path", "size"}), within the limits."""
    not_read: list[str] = []

    def within_limits(kind: list[dict], most: int) -> list[dict]:
        kept = []
        for file in kind:
            if file["size"] > MAX_FILE_BYTES:
                not_read.append(f"{file['name']}: over {MAX_FILE_BYTES // 1_000_000} MB")
            elif len(kept) >= most:
                not_read.append(f"{file['name']}: more than {most} of its kind")
            else:
                kept.append(file)
        return kept

    pdfs = within_limits([file for file in files if Path(file["name"]).suffix.lower() == ".pdf"], MAX_PDFS)
    spreadsheets = within_limits(
        [file for file in files if Path(file["name"]).suffix.lower() in SPREADSHEET_TYPES], MAX_SPREADSHEETS
    )
    images = within_limits(
        [file for file in files if Path(file["name"]).suffix.lower() in IMAGE_TYPES and file["size"] >= MIN_IMAGE_BYTES],
        MAX_IMAGES,
    )

    def text_of(file: dict, read_text) -> str:
        try:
            return read_text(file["path"])
        except NoNumberColumn:
            not_read.append(f"{file['name']}: no connote or reference column")
        except Exception as error:  # one unreadable file never stops the others
            not_read.append(f"{file['name']}: could not be read ({type(error).__name__})")
        return ""

    def numbers_in_cells(text: str) -> list[str]:
        # The cells go through the email text's own scheme: the regex, then lists written short (D-034).
        found = entity_extractor.extract_email_connotes("", text, stop_at_signoff=False)
        numbers, unclear = number_lists.expand(text, found)
        not_read.extend(f"a list in a spreadsheet could not be read: {item}" for item in unclear)
        return numbers

    sources = [
        ("PDF file name", _numbers_in_names([Path(file["name"]).stem for file in pdfs])),
        ("spreadsheet", numbers_in_cells("\n".join(text_of(file, spreadsheet_text) for file in spreadsheets))),
        ("picture", _numbers_in_text("\n".join(text_of(file, ocr_image) for file in images), number_model)),
        ("PDF content", _numbers_in_text("\n".join(text_of(file, pdf_text) for file in pdfs), number_model)),
    ]
    return {"sources": [{"source": name, "numbers": numbers} for name, numbers in sources if numbers],
            "not_read": not_read}


def _numbers_in_names(stems: list[str]) -> list[str]:
    """Numbers in file names: whole parts that look like a connote or reference, as in scan names (D-042)."""
    found = []
    for stem in stems:
        parts = pod_files.name_parts(stem)
        # A prefix written apart counts joined ("AB-10234" is AB10234); a date's parts never join up.
        joined = [first + second for first, second in zip(parts, parts[1:]) if first.isalpha() and second.isdigit()]
        found += [part for part in parts + joined if entity_extractor.looks_like_number(part)]
    return list(dict.fromkeys(found))


def _numbers_in_text(text: str, number_model) -> list[str]:
    """Numbers in a picture's or a document's text: the CRF at the surer threshold, else the regex."""
    if not text.strip():
        return []
    if number_model is not None:
        numbers = number_crf.extract(number_model, "", text, DOCUMENT_THRESHOLD)
        if numbers:
            return numbers
    return entity_extractor.extract_email_connotes("", text, stop_at_signoff=False)


class NoNumberColumn(ValueError):
    """A spreadsheet with no column headed as a connote or reference."""


def spreadsheet_text(path: Path) -> str:
    """The cells under a connote or reference header, one per line, from the first sheets."""
    texts = []
    for rows in _sheets(Path(path)):
        rows = [[_cell_text(value) for value in row] for row in rows]
        header = next((index for index, row in enumerate(rows[:HEADER_ROWS])
                       if any(_is_number_header(cell) for cell in row)), None)
        if header is None:
            continue
        columns = [index for index, cell in enumerate(rows[header]) if _is_number_header(cell)]
        texts += [row[index] for row in rows[header + 1:] for index in columns if index < len(row) and row[index]]
    if not texts:
        raise NoNumberColumn(path.name)
    return "\n".join(texts)


def _is_number_header(cell: str) -> bool:
    return bool(NUMBER_HEADER.search(re.sub(r"[^a-z0-9]+", " ", cell.lower())))


def _cell_text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # Excel keeps numbers as floats: 1234567.0 is 1234567
    return "" if value is None else str(value).strip()


def _sheets(path: Path) -> list[list[list]]:
    """The rows of a spreadsheet's first `MAX_SHEETS` sheets."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")  # saved by Excel on Windows
        return [list(csv.reader(io.StringIO(text)))]
    if suffix == ".xls":
        import xlrd

        book = xlrd.open_workbook(str(path), on_demand=True)
        try:
            sheets = [book.sheet_by_index(index) for index in range(min(book.nsheets, MAX_SHEETS))]
            return [[sheet.row_values(row) for row in range(sheet.nrows)] for sheet in sheets]
        finally:
            book.release_resources()
    from openpyxl import load_workbook

    book = load_workbook(str(path), read_only=True, data_only=True)
    try:
        return [[list(row) for row in sheet.iter_rows(values_only=True)] for sheet in book.worksheets[:MAX_SHEETS]]
    finally:
        book.close()


def ocr_image(path: Path) -> str:
    """The text in a picture, read by Windows' own OCR on this PC: nothing is sent anywhere."""
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import FileAccessMode, StorageFile

    async def recognise() -> str:
        file = await StorageFile.get_file_from_path_async(str(Path(path).resolve()))
        stream = await file.open_async(FileAccessMode.READ)
        bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("Windows has no OCR language installed")
        return (await engine.recognize_async(bitmap)).text

    return asyncio.run(recognise())


def pdf_text(path: Path) -> str:
    """The text of a PDF's first pages; a scanned PDF's pages are read by OCR."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES])
    if text.strip():
        return text
    import pypdfium2

    document = pypdfium2.PdfDocument(str(path))
    try:
        texts = []
        for number in range(min(len(document), MAX_PDF_PAGES)):
            page_image = Path(path).with_name(f"{Path(path).stem}_page{number + 1}.png")
            document[number].render(scale=2).to_pil().save(page_image)
            texts.append(ocr_image(page_image))
        return "\n".join(texts)
    finally:
        document.close()
