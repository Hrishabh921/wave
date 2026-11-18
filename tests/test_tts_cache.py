"""
tests/test_tts_cache.py — Tier 3 item #7: voice/tts_cache.py's
PhraseCache in isolation.

Pure unit tests - no pygame/edge_tts, no VoiceEngine. See
test_phase5_tts_fallback.py's TTSCacheIntegrationTests for _speak_task
actually consulting/populating this.

    python -m unittest discover -s tests -p "test_tts_cache.py"
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from settings.config import config                     # noqa: E402
from voice.tts_cache import PhraseCache                  # noqa: E402


class PhraseCacheTests(unittest.TestCase):
    def setUp(self):
        old_enabled = config.TTS_PHRASE_CACHE_ENABLED
        config.TTS_PHRASE_CACHE_ENABLED = True
        self.addCleanup(setattr, config, "TTS_PHRASE_CACHE_ENABLED", old_enabled)
        self.cache = PhraseCache(max_entries=3, max_text_len=20)

    def test_miss_on_an_empty_cache(self):
        self.assertIsNone(self.cache.get(("hi", "voiceA", "+0%")))

    def test_put_then_get_round_trips(self):
        key = ("hi", "voiceA", "+0%")
        self.cache.put(key, b"AUDIO")
        self.assertEqual(self.cache.get(key), b"AUDIO")

    def test_different_voice_is_a_different_key(self):
        self.cache.put(("hi", "voiceA", "+0%"), b"A")
        self.assertIsNone(self.cache.get(("hi", "voiceB", "+0%")))

    def test_different_rate_is_a_different_key(self):
        self.cache.put(("hi", "voiceA", "+0%"), b"A")
        self.assertIsNone(self.cache.get(("hi", "voiceA", "+10%")))

    def test_text_longer_than_max_len_is_never_stored(self):
        long_text = "x" * 21   # cache built with max_text_len=20 above
        key = (long_text, "voiceA", "+0%")
        self.cache.put(key, b"AUDIO")
        self.assertIsNone(self.cache.get(key))

    def test_text_at_exactly_max_len_is_stored(self):
        text = "x" * 20
        key = (text, "voiceA", "+0%")
        self.cache.put(key, b"AUDIO")
        self.assertEqual(self.cache.get(key), b"AUDIO")

    def test_empty_audio_is_not_stored(self):
        key = ("hi", "voiceA", "+0%")
        self.cache.put(key, b"")
        self.assertIsNone(self.cache.get(key))

    def test_lru_eviction_drops_the_least_recently_used_entry(self):
        """max_entries=3 in setUp - a 4th distinct key must evict exactly
        one, and it must be the one least recently touched."""
        self.cache.put(("a", "v", "r"), b"A")
        self.cache.put(("b", "v", "r"), b"B")
        self.cache.put(("c", "v", "r"), b"C")
        self.cache.put(("d", "v", "r"), b"D")   # should evict "a" - never re-touched since
        self.assertIsNone(self.cache.get(("a", "v", "r")))
        self.assertEqual(self.cache.get(("b", "v", "r")), b"B")
        self.assertEqual(self.cache.get(("c", "v", "r")), b"C")
        self.assertEqual(self.cache.get(("d", "v", "r")), b"D")

    def test_a_get_counts_as_recently_used_and_protects_from_eviction(self):
        self.cache.put(("a", "v", "r"), b"A")
        self.cache.put(("b", "v", "r"), b"B")
        self.cache.put(("c", "v", "r"), b"C")
        self.cache.get(("a", "v", "r"))         # touch "a" - "b" is now the oldest
        self.cache.put(("d", "v", "r"), b"D")    # should evict "b", not "a"
        self.assertIsNone(self.cache.get(("b", "v", "r")))
        self.assertEqual(self.cache.get(("a", "v", "r")), b"A")

    def test_re_putting_an_existing_key_refreshes_its_recency(self):
        self.cache.put(("a", "v", "r"), b"A")
        self.cache.put(("b", "v", "r"), b"B")
        self.cache.put(("c", "v", "r"), b"C")
        self.cache.put(("a", "v", "r"), b"A2")   # re-put, not just get
        self.cache.put(("d", "v", "r"), b"D")     # should evict "b", "a" was just refreshed
        self.assertIsNone(self.cache.get(("b", "v", "r")))
        self.assertEqual(self.cache.get(("a", "v", "r")), b"A2")

    def test_clear_empties_the_cache_and_resets_stats(self):
        self.cache.put(("a", "v", "r"), b"A")
        self.cache.get(("a", "v", "r"))
        self.cache.get(("missing", "v", "r"))
        self.cache.clear()
        self.assertIsNone(self.cache.get(("a", "v", "r")))   # "a" is gone too - clear() empties the data, not just the counters
        stats = self.cache.stats()
        self.assertEqual((stats.hits, stats.misses, stats.size), (0, 1, 0))   # the get() just above is a miss - it's the only post-clear access

    def test_stats_track_hits_misses_and_size(self):
        self.cache.put(("a", "v", "r"), b"A")
        self.cache.get(("a", "v", "r"))          # hit
        self.cache.get(("a", "v", "r"))          # hit
        self.cache.get(("missing", "v", "r"))    # miss
        stats = self.cache.stats()
        self.assertEqual(stats.hits, 2)
        self.assertEqual(stats.misses, 1)
        self.assertEqual(stats.size, 1)
        self.assertAlmostEqual(stats.hit_rate, 2 / 3)

    def test_hit_rate_is_zero_with_no_accesses_yet(self):
        self.assertEqual(self.cache.stats().hit_rate, 0.0)

    def test_disabled_flag_makes_get_and_put_both_no_ops(self):
        config.TTS_PHRASE_CACHE_ENABLED = False
        key = ("hi", "v", "r")
        self.cache.put(key, b"AUDIO")
        self.assertIsNone(self.cache.get(key))
        self.assertEqual(self.cache.stats().size, 0)

    def test_defaults_come_from_live_config_when_not_passed_explicitly(self):
        old_size = config.TTS_PHRASE_CACHE_SIZE
        config.TTS_PHRASE_CACHE_SIZE = 7
        self.addCleanup(setattr, config, "TTS_PHRASE_CACHE_SIZE", old_size)
        cache = PhraseCache()   # no explicit max_entries
        self.assertEqual(cache.max_entries, 7)

    def test_each_instance_has_its_own_independent_state(self):
        """Not a singleton-by-accident - voice/tts.py's module-level
        `phrase_cache` is ONE instance, but nothing stops a second one
        from existing (tests rely on this for isolation)."""
        other = PhraseCache(max_entries=3, max_text_len=20)
        self.cache.put(("a", "v", "r"), b"A")
        self.assertIsNone(other.get(("a", "v", "r")))


if __name__ == "__main__":
    unittest.main()
