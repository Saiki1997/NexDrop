"""3.1 quality options: stream variants up to a chosen height, subtitles in chosen languages saved
beside the video, and the session's checks on the options."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "engine" / "vendor"))

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="subs/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Español",LANGUAGE="es",URI="subs/es.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,SUBTITLES="subs"
1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,SUBTITLES="subs"
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,SUBTITLES="subs"
360/index.m3u8
"""


class Quality(unittest.TestCase):
    def setUp(self):
        import bridge
        from cyberdrop_dl.downloader import hls
        from cyberdrop_dl.utils import m3u8

        self.bridge, self.hls, self.m3u8 = bridge, hls, m3u8
        self.saved = (m3u8.VariantM3U8Parser.get_best_group, m3u8.RenditionDetails.new, hls.download)
        self.events = []
        self._emit = bridge.emit
        bridge.emit = self.events.append

    def tearDown(self):
        self.m3u8.VariantM3U8Parser.get_best_group, new, self.hls.download = self.saved
        self.m3u8.RenditionDetails.new = staticmethod(new)
        self.bridge.emit = self._emit

    def pick(self, payload):
        self.bridge._install_quality(payload)
        master = self.m3u8.M3U8(MASTER, self.bridge_url("https://cdn.test/v/master.m3u8"))
        return self.m3u8.select_best_rendition(master)

    def bridge_url(self, text):
        from cyberdrop_dl.utils import parse_url

        return parse_url(text, trim=False)

    def test_best_and_capped_quality(self):
        self.assertEqual(self.pick({}).resolution.height, 1080)
        self.tearDown(); self.setUp()  # noqa: E702
        self.assertEqual(self.pick({"videoQuality": "720"}).resolution.height, 720)
        self.assertIn("720p of 1080p, 720p, 360p", " ".join(e.get("message", "") for e in self.events))
        self.tearDown(); self.setUp()  # noqa: E702
        self.assertEqual(self.pick({"videoQuality": "480"}).resolution.height, 360)
        self.tearDown(); self.setUp()  # noqa: E702
        self.assertEqual(self.pick({"videoQuality": "smallest"}).resolution.height, 360)
        groups = [type("G", (), {"resolution": type("R", (), {"height": h})()})() for h in (2160, 1440)]
        self.assertEqual(self.bridge.pick_rendition(groups, "720").resolution.height, 1440)  # all above the cap: the smallest

    def test_subtitles_off_by_default_and_by_language(self):
        self.assertIsNone(self.pick({}).urls.subtitle)
        self.assertIn("Subtitles offered: en, es", " ".join(e.get("message", "") for e in self.events))
        self.tearDown(); self.setUp()  # noqa: E702
        chosen = self.pick({"subtitles": {"on": True, "languages": ["es", "en"]}})
        self.assertTrue(str(chosen.urls.subtitle).endswith("subs/es.m3u8"))
        self.tearDown(); self.setUp()  # noqa: E702
        self.assertIsNone(self.pick({"subtitles": {"on": True, "languages": ["fr"]}}).urls.subtitle)

    def test_subtitles_are_saved_beside_the_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            temp = base / "clip.mp4.hls"
            temp.mkdir()
            sub = temp / "clip.subtitle.vtt"
            sub.write_text("WEBVTT\n")
            (base / "clip.es.vtt").write_text("older")  # never overwritten

            async def original(media_item, rendition, download_fn, client):
                return self.hls.Streams(base / "v.ts", None, sub)

            self.hls.download = original
            self.bridge._install_quality({"subtitles": {"on": True}})
            source = "https://cdn.test/v/subs/es.m3u8"
            self.bridge.SUB_LANGUAGE[source] = "es"
            item = type("M", (), {"path": base / "clip.mp4", "url": "x"})()
            rendition = type("R", (), {"subtitle": type("S", (), {"source": source})()})()
            streams = asyncio.run(self.hls.download(item, rendition, None, None))
            self.assertEqual(streams.subs, base / "clip.es (2).vtt")
            self.assertEqual((base / "clip.es (2).vtt").read_text(), "WEBVTT\n")
            self.assertEqual((base / "clip.es.vtt").read_text(), "older")
            self.assertFalse(temp.exists())


class SessionChecks(unittest.TestCase):
    def test_options_are_checked(self):
        import cairn_session

        out = cairn_session.quality_options({"videoQuality": "9999", "subtitles": {"on": 1, "languages": ["EN", "e s!", "x" * 40]}})
        self.assertEqual(out["videoQuality"], "best")
        self.assertEqual(out["subtitles"], {"on": True, "languages": ["en", "es", "x" * 12]})
        self.assertEqual(cairn_session.quality_options({})["subtitles"], {"on": False, "languages": []})


if __name__ == "__main__":
    unittest.main()
