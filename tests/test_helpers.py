import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "speed_up_media.py"

spec = importlib.util.spec_from_file_location("speed_up_media", SCRIPT_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load speed_up_media.py for tests")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FileSelectionTests(unittest.TestCase):
    def test_comma_separated_numbers_and_ranges(self):
        self.assertEqual(
            module.parse_file_selection("1, 3, 6-11", 19),
            [1, 3, 6, 7, 8, 9, 10, 11],
        )

    def test_duplicate_numbers_are_removed(self):
        self.assertEqual(module.parse_file_selection("1, 2-4, 3", 5), [1, 2, 3, 4])

    def test_invalid_ranges_are_rejected(self):
        for selection in ("0", "1-6", "4-2", "one", ""):
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    module.parse_file_selection(selection, 5)


class TimingTests(unittest.TestCase):
    def test_srt_timestamp_is_scaled(self):
        self.assertEqual(
            module.convert_subtitle_timestamp("00:00:03,000", 1.5, "SRT"),
            "00:00:02,000",
        )

    def test_ass_timestamp_is_scaled(self):
        self.assertEqual(
            module.convert_subtitle_timestamp("0:00:03.00", 1.5, "ASS"),
            "0:00:02.00",
        )

    def test_progress_percent_uses_output_time(self):
        self.assertEqual(
            module.parse_progress_block({"out_time_us": "1500000"}, 3.0),
            50.0,
        )

    def test_progress_end_is_one_hundred_percent(self):
        self.assertEqual(module.parse_progress_block({"progress": "end"}, 10.0), 100.0)

    def test_chapter_metadata_is_scaled(self):
        chapters = [{
            "start_time": "0.0",
            "end_time": "4.0",
            "tags": {"title": "Intro"},
        }]
        with tempfile.TemporaryDirectory() as directory:
            metadata = module.create_scaled_chapter_metadata(chapters, 2.0, Path(directory))
            self.assertIsNotNone(metadata)
            text = metadata.read_text(encoding="utf-8")
            self.assertIn("START=0", text)
            self.assertIn("END=2000", text)
            self.assertIn("title=Intro", text)


class SubtitleFileTests(unittest.TestCase):
    def test_cp1251_subtitle_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.srt"
            path.write_bytes("\u0422\u0435\u0441\u0442 \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u043e\u0432".encode("cp1251"))
            self.assertEqual(module.read_subtitle_text(path), "\u0422\u0435\u0441\u0442 \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u043e\u0432")

    def test_external_srt_is_written_as_utf8_with_scaled_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            destination = Path(directory) / "output.srt"
            source.write_text("1\n00:00:03,000 --> 00:00:06,000\nHello\n", encoding="utf-8")
            module.scale_external_subtitle_file(source, destination, 2.0)
            result = destination.read_text(encoding="utf-8")
            self.assertIn("00:00:01,500 --> 00:00:03,000", result)


if __name__ == "__main__":
    unittest.main()
