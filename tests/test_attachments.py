"""Numbers from attachments when the email's text has none (D-044)."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import attachments, outlook_reader

HAS_WINDOWS_OCR = importlib.util.find_spec("winrt") is not None


def _file(folder: Path, name: str, size: int = 50_000) -> dict:
    path = folder / name
    path.write_bytes(b"x")
    return {"name": name, "path": path, "size": size}


class NumbersFromTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_the_sources_come_in_the_owners_order_and_only_with_numbers(self):
        files = [_file(self.folder, "7000003-80600001 - Signed.pdf"), _file(self.folder, "screen.png")]
        with mock.patch.object(attachments, "ocr_image", return_value="Connote 1234567"), \
                mock.patch.object(attachments, "pdf_text", return_value=""):
            found = attachments.numbers_from(files, None)
        self.assertEqual(found["sources"], [
            {"source": "PDF file name", "numbers": ["7000003", "80600001"]},
            {"source": "picture", "numbers": ["1234567"]},
        ])
        self.assertEqual(found["not_read"], [])

    @mock.patch.dict(os.environ, {"POD_CONNOTE_PREFIXES": "AB;XY;KL"})  # the prefixes are set in .env (D-060)
    def test_a_date_in_a_file_name_is_never_a_number(self):
        files = [_file(self.folder, "Invoice 07_04_2026.pdf"), _file(self.folder, "AB-10234 manifest.pdf")]
        with mock.patch.object(attachments, "pdf_text", return_value=""):
            found = attachments.numbers_from(files, None)
        self.assertEqual(found["sources"], [{"source": "PDF file name", "numbers": ["AB10234"]}])

    def test_limits_leave_files_unread_and_say_why(self):
        files = [_file(self.folder, "big.pdf", 11_000_000), _file(self.folder, "logo.png", 5_000)]
        files += [_file(self.folder, f"page{n}.png") for n in range(6)]
        with mock.patch.object(attachments, "ocr_image", return_value="") as ocr, \
                mock.patch.object(attachments, "pdf_text", return_value=""):
            found = attachments.numbers_from(files, None)
        self.assertEqual(ocr.call_count, attachments.MAX_IMAGES)  # the small logo is not read at all
        self.assertIn("big.pdf: over 10 MB", found["not_read"])
        self.assertIn("page5.png: more than 5 of its kind", found["not_read"])
        self.assertEqual(found["sources"], [])

    def test_a_file_that_cannot_be_read_is_reported_and_the_others_still_count(self):
        files = [_file(self.folder, "broken.png"), _file(self.folder, "ok.png")]
        with mock.patch.object(attachments, "ocr_image", side_effect=[OSError("bad"), "POD for 1234567"]), \
                mock.patch.object(attachments, "pdf_text", return_value=""):
            found = attachments.numbers_from(files, None)
        self.assertEqual(found["sources"], [{"source": "picture", "numbers": ["1234567"]}])
        self.assertEqual(found["not_read"], ["broken.png: could not be read (OSError)"])


class SpreadsheetTest(unittest.TestCase):
    """Numbers from the connote and reference columns of a spreadsheet (product owner, D-046)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def workbook(self, name: str, rows: list[list]) -> dict:
        from openpyxl import Workbook

        book = Workbook()
        for row in rows:
            book.active.append(row)
        path = self.folder / name
        book.save(path)
        return {"name": name, "path": path, "size": path.stat().st_size}

    def test_only_the_number_columns_are_read_below_their_header_through_the_text_scheme(self):
        rows = [["Delivery list"], [], ["Item", "Con Note", "Invoice", "Qty", "Customer Ref"],
                ["Fish", 1234567, 20260407, 12, "80600001"], ["Milk", "7335842/843", 20260408, 3, None]]
        found = attachments.numbers_from([self.workbook("list.xlsx", rows)], None)
        self.assertEqual(found["sources"], [
            {"source": "spreadsheet", "numbers": ["1234567", "80600001", "7335842", "7335843"]}])
        self.assertEqual(found["not_read"], [])

    def test_every_number_is_read_with_no_limit(self):
        rows = [["Connote"], *[[2000000 + n] for n in range(30)]]
        found = attachments.numbers_from([self.workbook("many.xlsm", rows)], None)
        self.assertEqual(len(found["sources"][0]["numbers"]), 30)

    def test_a_csv_saved_by_excel_is_read(self):
        path = self.folder / "list.csv"
        path.write_bytes("Café,DO Number\nx,3000021\n".encode("cp1252"))
        found = attachments.numbers_from([{"name": "list.csv", "path": path, "size": 30}], None)
        self.assertEqual(found["sources"], [{"source": "spreadsheet", "numbers": ["3000021"]}])

    def test_an_old_xls_is_read_with_xlrd(self):
        sheet = SimpleNamespace(nrows=2, row_values=lambda row: [["Reference", "Qty"], [80600002.0, 4.0]][row])
        book = SimpleNamespace(nsheets=1, sheet_by_index=lambda index: sheet, release_resources=mock.Mock())
        xlrd = SimpleNamespace(open_workbook=mock.Mock(return_value=book))
        path = self.folder / "old.xls"
        path.write_bytes(b"x")
        with mock.patch.dict(sys.modules, {"xlrd": xlrd}):
            found = attachments.numbers_from([{"name": "old.xls", "path": path, "size": 1}], None)
        self.assertEqual(found["sources"], [{"source": "spreadsheet", "numbers": ["80600002"]}])
        book.release_resources.assert_called_once()

    def test_a_spreadsheet_without_a_number_column_is_reported(self):
        found = attachments.numbers_from([self.workbook("prices.xlsx", [["Item", "Price"], ["Fish", 1234567]])], None)
        self.assertEqual(found, {"sources": [], "not_read": ["prices.xlsx: no connote or reference column"]})

    def test_the_spreadsheet_comes_after_the_pdf_names_and_before_the_pictures(self):
        files = [self.workbook("list.xlsx", [["Connote"], [1234567]]),
                 _file(self.folder, "7000003.pdf"), _file(self.folder, "screen.png")]
        with mock.patch.object(attachments, "ocr_image", return_value="Connote 2345678"), \
                mock.patch.object(attachments, "pdf_text", return_value=""):
            found = attachments.numbers_from(files, None)
        self.assertEqual([source["source"] for source in found["sources"]], ["PDF file name", "spreadsheet", "picture"])

    def test_the_header_words_the_owner_named(self):
        for header in ("Connote", "Con Note", "CONSIGNMENT NO", "DO Number", "DoNumber", "Reference",
                       "Customer Ref", "Order No"):
            self.assertTrue(attachments._is_number_header(header), header)
        for header in ("Preferred", "Delivery Date", "Qty", "Order Date"):
            self.assertFalse(attachments._is_number_header(header), header)


class PdfTextTest(unittest.TestCase):
    def test_a_text_pdf_is_read_without_ocr(self):
        stream = b"BT /F1 12 Tf 72 712 Td (Connote 1234567) Tj ET"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        body, offsets = b"%PDF-1.4\n", []
        for number, content in enumerate(objects, start=1):
            offsets.append(len(body))
            body += b"%d 0 obj\n" % number + content + b"\nendobj\n"
        xref = len(body)
        body += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
        body += b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.pdf"
            path.write_bytes(body)
            with mock.patch.object(attachments, "ocr_image", side_effect=AssertionError("no OCR for text")):
                self.assertIn("1234567", attachments.pdf_text(path))


@unittest.skipUnless(HAS_WINDOWS_OCR, "Windows OCR (winrt) is not installed")
class WindowsOcrTest(unittest.TestCase):
    """Windows' own OCR on pictures made here: nothing leaves the PC."""

    def picture(self, folder: Path, name: str) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (900, 200), "white")
        try:
            font = ImageFont.truetype("arial.ttf", 34)
        except OSError:
            font = ImageFont.load_default()
        ImageDraw.Draw(image).text((20, 60), "Please send POD for 1234567", fill="black", font=font)
        path = folder / name
        image.save(path)
        return path

    def test_a_picture_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn("1234567", attachments.ocr_image(self.picture(Path(folder), "screen.png")))

    def test_a_scanned_pdf_is_read_through_its_pages(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            picture = self.picture(Path(folder), "page.png")
            pdf = Path(folder) / "scan.pdf"
            Image.open(picture).convert("RGB").save(pdf)  # a PDF with no text, only a picture
            self.assertIn("1234567", attachments.pdf_text(pdf))


class SaveAttachmentsTest(unittest.TestCase):
    def test_only_pdfs_spreadsheets_and_pictures_are_saved_under_names_made_here(self):
        saved_to = []

        def attachment(name, kind=1):
            return SimpleNamespace(FileName=name, Type=kind, Size=1000,
                                   SaveAsFile=lambda path: (saved_to.append(path), Path(path).write_bytes(b"x")))

        items = [attachment("..\\..\\escape.pdf"), attachment("photo.JPG"), attachment("notes.docx"),
                 attachment("forwarded.msg", kind=5), attachment("list.xlsx"), attachment("old.XLS")]
        item = SimpleNamespace(Attachments=SimpleNamespace(Count=len(items), Item=lambda n: items[n - 1]))
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(outlook_reader, "_open", return_value=item):
            saved = outlook_reader.save_attachments("E1", "POD_STORE", Path(folder))
            self.assertEqual([file["name"] for file in saved], ["escape.pdf", "photo.JPG", "list.xlsx", "old.XLS"])
            self.assertTrue(all(Path(path).parent == Path(folder) for path in saved_to))


if __name__ == "__main__":
    unittest.main()
