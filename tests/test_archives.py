"""3.1 archive inspection and safe extraction: path traversal, links, bombs, damage, no overwrite."""

import io
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

import archives  # noqa: E402


class Archives(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def zip(self, name, files, **kw):
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", compression=kw.get("compression", zipfile.ZIP_DEFLATED)) as archive:
            for entry, data in files.items():
                archive.writestr(entry, data)
        return path

    def test_names_that_leave_the_folder_are_refused(self):
        for name in ("../evil.txt", "/abs.txt", "C:/x.txt", "a/../../b", "..\\win.txt", "con.txt"):
            self.assertIsNone(archives.safe_name(name), name)
        self.assertEqual(archives.safe_name("ok/"), "ok")
        self.assertEqual(archives.safe_name("album/pic 1.jpg"), "album/pic 1.jpg")

    def test_inspect_lists_files_and_problems(self):
        path = self.zip("a.zip", {"album/1.jpg": b"x" * 100, "../evil.txt": b"bad", "run.exe": b"MZ"})
        report = archives.inspect(path)
        self.assertTrue(report["ok"])
        self.assertEqual(report["files"], 3)
        self.assertIn("outside the folder", report["problems"][0])
        self.assertEqual(report["runnable"], ["run.exe"])

    def test_extract_writes_only_safe_files_and_never_runs_anything(self):
        path = self.zip("a.zip", {"album/1.jpg": b"one", "../evil.txt": b"bad", "album/2.jpg": b"two"})
        dest = self.tmp / "out"
        result = archives.extract(path, dest)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["written"], result["skipped"]), (2, 1))
        self.assertEqual((dest / "album" / "2.jpg").read_bytes(), b"two")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_tar_links_and_traversal_are_skipped(self):
        path = self.tmp / "a.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in (("ok.txt", b"fine"), ("../up.txt", b"bad")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
        result = archives.extract(path, self.tmp / "out")
        self.assertEqual((result["written"], result["skipped"]), (1, 2))
        self.assertFalse((self.tmp / "out" / "link").exists())

    def test_decompression_bomb_is_refused(self):
        path = self.zip("bomb.zip", {"zeros.bin": b"\0" * (300 * 1024 * 1024)})
        self.assertLess(path.stat().st_size, 2 * 1024 * 1024)
        self.assertTrue(any("bomb" in p for p in archives.inspect(path)["problems"]))
        result = archives.extract(path, self.tmp / "out")
        self.assertFalse(result["ok"])
        self.assertFalse((self.tmp / "out").exists())

    def test_nothing_is_overwritten(self):
        path = self.zip("a.zip", {"1.jpg": b"one"})
        dest = self.tmp / "out"
        dest.mkdir()
        (dest / "keep.txt").write_text("mine")
        self.assertFalse(archives.extract(path, dest)["ok"])
        self.assertEqual((dest / "keep.txt").read_text(), "mine")

    def test_damaged_archive_is_reported(self):
        path = self.zip("a.zip", {"1.bin": bytes(range(256)) * 400}, compression=zipfile.ZIP_STORED)
        data = bytearray(path.read_bytes())
        data[200] ^= 0xFF  # inside the stored file data: the CRC no longer matches
        path.write_bytes(bytes(data))
        result = archives.test(path)
        self.assertFalse(result["good"])
        good = self.zip("b.zip", {"1.bin": b"abc"})
        self.assertTrue(archives.test(good)["good"])

    def test_other_formats_are_not_guessed(self):
        path = self.tmp / "x.7z"
        path.write_bytes(b"7z\xbc\xaf'\x1c" + b"\0" * 30)
        self.assertIn("7-Zip", archives.inspect(path)["error"])
        text = self.tmp / "x.txt"
        text.write_text("hello")
        self.assertFalse(archives.inspect(text)["ok"])


if __name__ == "__main__":
    unittest.main()
