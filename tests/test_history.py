import tempfile
import unittest
from pathlib import Path

import baixar_legenda


class HistoryTest(unittest.TestCase):
    def test_attempt_is_scoped_by_movie_hash_and_file_id(self):
        with tempfile.TemporaryDirectory() as directory:
            baixar_legenda.CONFIG_DIR = Path(directory)
            baixar_legenda.DB_FILE = Path(directory) / "history.db"
            conn = baixar_legenda.init_db()
            subtitle = baixar_legenda.Subtitle(
                file_id=123,
                subtitle_id="456",
                language="pt-br",
                release="release",
                download_count=10,
                new_download_count=2,
                rating=8.0,
                hearing_impaired=False,
                ai_translated=False,
                machine_translated=False,
                from_trusted=True,
                moviehash_match=True,
                file_name="movie.srt",
                fps=23.976,
            )
            baixar_legenda.mark_attempted(conn, "hash-a", subtitle)
            self.assertTrue(baixar_legenda.was_attempted(conn, "hash-a", 123))
            self.assertFalse(baixar_legenda.was_attempted(conn, "hash-a", 999))
            self.assertFalse(baixar_legenda.was_attempted(conn, "hash-b", 123))
            conn.close()


if __name__ == "__main__":
    unittest.main()
