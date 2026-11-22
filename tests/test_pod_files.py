"""Finding scans on the share by the whole numbers in their file names (D-003, D-042)."""

import tempfile
import unittest
from pathlib import Path

from app import pod_files


def _index(*names):
    """An index of these file names, as the share gives them."""
    return pod_files.FileIndex((), tuple((Path(name).stem, Path(name)) for name in names), 0)


class PodFilesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "2026"
        for relative in (
            "Jan/12345_absd.pdf",
            "Jan/deep/AB-10234_102cd_x1y2345.PDF",
            "Feb/123456_xyz.pdf",
            "Feb/7654321 scan.pdf",
            "Feb/7654321.txt",  # not a PDF
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF")
        self.index = pod_files.build_index([self.root])

    def test_cleaning_removes_spaces_dashes_commas_dots_and_uppercases(self):
        self.assertEqual(pod_files.clean(" ab-10.23,4 "), "AB10234")

    def test_one_matching_file_is_found_in_any_subfolder(self):
        result = pod_files.search(self.index, ["AB 10234", "7654321"])
        self.assertEqual(result.found["AB 10234"].name, "AB-10234_102cd_x1y2345.PDF")
        self.assertEqual(result.found["7654321"].name, "7654321 scan.pdf")
        self.assertEqual(result.ambiguous, {})

    def test_a_number_inside_a_longer_number_is_never_its_scan(self):
        # 12345_absd.pdf is the scan; 123456_xyz.pdf is another number (product owner, D-042).
        self.assertEqual(pod_files.search(self.index, ["12345"]).found["12345"].name, "12345_absd.pdf")
        # A pallet number holding the requested digits is not a match at all.
        result = pod_files.search(_index("PLT73985078J - Signed.pdf"), ["7398507"])
        self.assertEqual((result.found, result.ambiguous, result.missing), ({}, {}, ["7398507"]))

    def test_the_scan_names_used_on_the_share(self):
        """Product owner: connote, then maybe a reference or a date, "_" or "-" between, then Signed and a copy number."""
        found = lambda names, number: pod_files.search(_index(*names), [number]).found.get(number, Path("-")).name
        self.assertEqual(found(["7000001 - Signed.pdf"], "7000001"), "7000001 - Signed.pdf")
        for separator in ("_", "-"):
            with self.subTest(separator=separator):
                with_reference = f"7000002{separator}80655501 - Signed.pdf"
                self.assertEqual(found([with_reference], "7000002"), with_reference)
                self.assertEqual(found([with_reference], "80655501"), with_reference)  # asked by its reference
                with_date = f"7000004{separator}07{separator}04{separator}2026 - Signed.pdf"
                self.assertEqual(found([with_date], "7000004"), with_date)
                # An invoice number that looks like the date never picks it.
                self.assertEqual(pod_files.search(_index(with_date), ["20260407", "07042026"]).missing, ["20260407", "07042026"])
        # A signed scan wins over the same number without the word.
        self.assertEqual(found(["7000006..pdf", "7000006 - Signed.pdf"], "7000006"), "7000006 - Signed.pdf")
        # Several signed copies: the team chooses.
        copies = ["7000008 - Signed.pdf", "7000008 - signed(2).pdf", "7000008 - Signed (3).pdf"]
        self.assertEqual(len(pod_files.search(_index(*copies), ["7000008"]).ambiguous["7000008"]), 3)
        # Prefixes written apart or joined, and a part number.
        self.assertEqual(found(["KL-0045678 - Signed.pdf"], "KL-0045678"), "KL-0045678 - Signed.pdf")
        self.assertEqual(found(["XY62302 - Signed.pdf"], "XY62302"), "XY62302 - Signed.pdf")
        self.assertEqual(found(["I 1234567 - Signed.pdf"], "I1234567"), "I 1234567 - Signed.pdf")
        self.assertEqual(found(["4653685-01 - Signed.pdf"], "4653685-01"), "4653685-01 - Signed.pdf")

    def test_unsigned_scans_are_never_indexed(self):
        for name in ("7000007..pdf", "7000007 - Unsigned.pdf", "7000009 - unsigned.pdf"):
            (self.root / "Mar" / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / "Mar" / name).write_bytes(b"%PDF")
        index = pod_files.build_index([self.root])
        self.assertEqual(pod_files.search(index, ["7000007"]).found["7000007"].name, "7000007..pdf")
        self.assertEqual(pod_files.search(index, ["7000009"]).missing, ["7000009"])

    def test_leading_zeros_match_either_way_and_an_exact_file_wins(self):
        index = _index

        padded = index("0089855504.pdf", "089855504_x.pdf")
        result = pod_files.search(padded, ["89855504"])
        # Two zero-padded copies and no exact file: the team chooses.
        self.assertEqual([p.name for p in result.ambiguous["89855504"]], ["0089855504.pdf", "089855504_x.pdf"])
        # An exact file wins over its zero-padded copies.
        with_exact = index("0089855504.pdf", "089855504_x.pdf", "89855504 scan.pdf")
        self.assertEqual(pod_files.search(with_exact, ["89855504"]).found["89855504"].name, "89855504 scan.pdf")
        # A zero-padded request finds the unpadded file.
        self.assertEqual(pod_files.search(index("89855504.pdf"), ["0089855504"]).found["0089855504"].name, "89855504.pdf")
        # A different number that only ends the same is not a zero variant.
        self.assertEqual(pod_files.search(index("189855504.pdf"), ["0089855504"]).missing, ["0089855504"])
        # A longer number is another number: the exact file is found (D-042).
        self.assertEqual(pod_files.search(index("89855504.pdf", "898555041.pdf"), ["89855504"]).found["89855504"].name, "89855504.pdf")

    def test_a_customer_folder_comes_first_and_elsewhere_needs_the_team(self):
        for relative in ("Customer/Sep/4444444.pdf", "Oct/4444444_copy.pdf", "Oct/5555555.pdf",
                         "Customer/6666666_a.pdf", "Customer/6666666_b.pdf"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF")
        index = pod_files.build_index([self.root])
        result = pod_files.search(index, ["4444444", "5555555", "6666666", "9999999"], folder="customer")
        # In the customer's folder: that file only, even though a copy exists elsewhere.
        self.assertEqual(result.found["4444444"].name, "4444444.pdf")
        # Only elsewhere: never sent without the team.
        self.assertEqual([p.name for p in result.ambiguous["5555555"]], ["5555555.pdf"])
        self.assertEqual(result.outside_folder, ["5555555"])
        # Two in the folder: the usual choice, not "outside".
        self.assertEqual(len(result.ambiguous["6666666"]), 2)
        self.assertEqual(result.missing, ["9999999"])
        # Without a folder, nothing changes.
        self.assertEqual(len(pod_files.search(index, ["4444444"]).ambiguous["4444444"]), 2)

    def test_no_matching_file_is_missing(self):
        self.assertEqual(pod_files.search(self.index, ["9999999"]).missing, ["9999999"])

    def test_non_pdf_files_are_ignored(self):
        self.assertEqual(len(self.index.entries), 4)

    def test_scan_excludes_absolute_and_root_relative_folders(self):
        excluded = self.root / "Archive"
        nested = excluded / "Old"
        nested.mkdir(parents=True)
        nested_named = self.root / "Current" / "Archive"
        nested_named.mkdir(parents=True)
        for path in (
            excluded / "1234567.pdf",
            nested / "7777777.pdf",
            nested_named / "8888888.pdf",
        ):
            path.write_bytes(b"%PDF")
        allowed = self.root / "Current" / "1234567.pdf"
        allowed.parent.mkdir(exist_ok=True)
        allowed.write_bytes(b"%PDF")

        absolute_index = pod_files.build_index([self.root], [excluded])
        self.assertEqual(pod_files.search(absolute_index, ["1234567"]).found["1234567"], allowed)
        self.assertEqual(pod_files.search(absolute_index, ["7777777"]).missing, ["7777777"])

        name_index = pod_files.build_index([self.root], [Path("Archive")])
        self.assertEqual(pod_files.search(name_index, ["7777777"]).missing, ["7777777"])
        self.assertEqual(pod_files.search(name_index, ["8888888"]).missing, ["8888888"])
        self.assertTrue(pod_files.is_excluded(
            nested_named / "8888888.pdf", [self.root], [Path("Archive")]
        ))

        relative_index = pod_files.build_index([self.root], [Path("Archive/Old")])
        self.assertEqual(pod_files.search(relative_index, ["7777777"]).missing, ["7777777"])
        self.assertIn("8888888", pod_files.search(relative_index, ["8888888"]).found)

    def test_excluded_folder_outside_scan_roots_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside POD_SCAN_FOLDERS"):
            pod_files.build_index([self.root], [Path(self._tmp.name) / "outside"])

    def test_unreachable_folder_raises_instead_of_finding_nothing(self):
        with self.assertRaises(FileNotFoundError):
            pod_files.build_index([self.root, Path(self._tmp.name) / "2027"])

    def test_only_files_under_the_scan_folders_count_as_inside(self):
        self.assertTrue(pod_files.is_inside(self.root / "Jan" / "12345_absd.pdf", [self.root]))
        self.assertFalse(pod_files.is_inside(Path(self._tmp.name) / "other.pdf", [self.root]))
        self.assertFalse(pod_files.is_inside(self.root / ".." / "other.pdf", [self.root]))

    def test_file_uri_path_is_normalized_and_resolved_only_inside_the_scan_index(self):
        path = self.root / "POD" / "Receiver" / "524037 - Signed.pdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"%PDF")
        index = pod_files.build_index([self.root])
        self.assertEqual(pod_files.indexed_path(index, path.as_uri()), path)
        self.assertIsNone(pod_files.indexed_path(index, (self.root.parent / "outside.pdf").as_uri()))

    def test_network_file_uri_is_converted_to_unc_path(self):
        self.assertEqual(
            pod_files.path_from_uri(
                "file://server/scans//2026/POD/Receiver/"
                "Delivery Dockets/524037 - Signed.pdf"
            ),
            Path(
                "\\\\server\\scans\\2026\\POD\\Receiver\\"
                "Delivery Dockets\\524037 - Signed.pdf"
            ),
        )


if __name__ == "__main__":
    unittest.main()
