import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import baixar_legenda
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

    def test_release_name_similarity_ranks_above_trust(self):
        video_name = (
            "The.Handmaiden.2016.EXTENDED.KOREAN.1080p.BluRay.H264.AAC-VXT"
        )
        exact_release = baixar_legenda.Subtitle(
            file_id=1,
            subtitle_id="1",
            language="pt-br",
            release=video_name,
            title="The Handmaiden",
            download_count=5000,
            new_download_count=500,
            rating=10.0,
            hearing_impaired=False,
            ai_translated=False,
            machine_translated=False,
            from_trusted=False,
            moviehash_match=False,
            file_name=video_name,
            fps=None,
        )
        trusted_release = baixar_legenda.Subtitle(
            file_id=2,
            subtitle_id="2",
            language="pt-br",
            release="The.Handmaiden.2016.[BluRay+BRRip+BDRip]",
            title="The Handmaiden",
            download_count=15000,
            new_download_count=800,
            rating=9.3,
            hearing_impaired=False,
            ai_translated=False,
            machine_translated=False,
            from_trusted=True,
            moviehash_match=False,
            file_name="trusted.srt",
            fps=23.976,
        )

        ranked = baixar_legenda.rank_subtitles(
            [trusted_release, exact_release],
            video_name,
        )

        self.assertEqual([subtitle.file_id for subtitle in ranked], [1, 2])
        self.assertEqual(
            baixar_legenda.subtitle_name_similarity(video_name, video_name),
            1.0,
        )

    def test_hash_match_is_first_but_title_results_are_also_returned(self):
        def subtitle(file_id, release, moviehash_match=False):
            return {
                "id": str(file_id),
                "attributes": {
                    "subtitle_id": str(file_id),
                    "language": "pt-BR",
                    "release": release,
                    "files": [{"file_id": file_id, "file_name": release}],
                    "moviehash_match": moviehash_match,
                    "from_trusted": True,
                },
            }

        class Response:
            status_code = 200

            def __init__(self, data):
                self.data = data

            def json(self):
                return {"data": self.data}

        session = requests.Session()
        responses = [
            Response([subtitle(1, "wrong release", moviehash_match=True)]),
            Response(
                [
                    subtitle(1, "wrong release"),
                    subtitle(2, "matching release"),
                    subtitle(3, "another release"),
                ]
            ),
        ]
        video = Path("The.Handmaiden.2016.1080p.mp4")
        with patch.object(session, "get", side_effect=responses) as get:
            with (
                patch.object(
                    baixar_legenda, "opensubtitles_hash", return_value="hash"
                ),
                patch.object(
                    baixar_legenda,
                    "identify_video",
                    return_value={"title": "The Handmaiden", "type": "movie"},
                ),
            ):
                results = baixar_legenda.search_subtitles(
                    session, video, "pt-br", 100
                )

        self.assertEqual([item.file_id for item in results], [1, 2, 3])
        self.assertTrue(results[0].moviehash_match)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            get.call_args_list[1].kwargs["params"]["query"], "The Handmaiden"
        )


if __name__ == "__main__":
    unittest.main()
