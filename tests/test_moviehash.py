import tempfile
import unittest
from pathlib import Path

from baixar_legenda import opensubtitles_hash


class MovieHashTest(unittest.TestCase):
    def test_hash_is_16_hex_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.bin"
            path.write_bytes(bytes(range(256)) * 1024)
            result = opensubtitles_hash(path)
            self.assertEqual(len(result), 16)
            int(result, 16)

    def test_small_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small.bin"
            path.write_bytes(b"x" * 100)
            with self.assertRaises(Exception):
                opensubtitles_hash(path)


if __name__ == "__main__":
    unittest.main()
