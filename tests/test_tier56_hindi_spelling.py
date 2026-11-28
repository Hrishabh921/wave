"""tests/test_tier56_hindi_spelling.py - Tier 56: Hindi commands spelled the
way Whisper writes them.

The fast path knows one spelling per Devanagari word; Whisper (medium, on
spoken Hindi commands) wrote the same words many ways. Every variant below
is one Whisper actually produced on the 2026-11-27 benchmark clips. Measured
through Wave's own speech path: Hindi commands right 15% -> 41%, English and
Hinglish unchanged; golden set still 100% with no false triggers.
"""
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402,F401  (adds the repo root to sys.path)

from brain import intent_router as ir  # noqa: E402


def _skill(text):
    r = ir.route(text)
    return r.skill if r else None


class WhisperSpellingTests(unittest.TestCase):
    def test_spelling_variants_reach_the_right_command(self):
        for heard, want in (
                ("इसे कोपी करो।", "copy_text"), ("इसे कौपी करो", "copy_text"),                 # ॉ written ो / ौ
                ("क्या समय हूआ है?", "get_current_time"),                                       # ू for ु
                ("जूम इन करो।", "zoom_in"),                                                     # no nukta
                ("पीसी शट्डाउन करो।", "shutdown_pc"),                                           # extra halant
                ("पीसी हाईबर्नेट करो।", "hibernate_pc"),                                        # ई, halant
                ("पेज रिलोड करो।", "reload_page"),                                              # ि for ी
                ("रीसाइकल बिन खोलो।", "open_recycle_bin"), ("रीसाएकल बिन खोलो", "open_recycle_bin"),
                ("वोल्यूम कम करो।", "volume_down"),
                ("कम्प्यूटर लॉक करो", "lock_pc"),                                               # म्प for ंप
                ("आवाज बढाओ", "volume_up")):                                                    # no nuktas
            with self.subTest(heard=heard):
                self.assertEqual(_skill(heard), want)

    def test_a_word_whisper_split_in_two_is_joined_back(self):
        for heard, want in (("पेज री लोड करो।", "reload_page"), ("पीसी हाई बरनेट करो।", "hibernate_pc"),
                            ("पीसी हाईबर नेट करो", "hibernate_pc")):
            with self.subTest(heard=heard):
                self.assertEqual(_skill(heard), want)

    def test_mishearings_stay_misses(self):
        self.assertIsNone(_skill("आवास तेस करो।"))          # आवाज़ तेज़ - heard wrong, not spelled differently
        self.assertIsNone(_skill("यहाँ पूर्ववत करो"))        # undo stays an exact match - "यहाँ" isn't "यह"


class SafetyTests(unittest.TestCase):
    def test_negation_still_blocks(self):
        self.assertIsNone(_skill("आवाज बढाओ मत"))
        self.assertIsNone(_skill("वोल्यूम कम मत करो"))

    def test_latin_text_is_never_touched(self):
        for text in ("Open Chrome.", "volume badhao", "set the volume to 5", "Take a note: call MOM at 5"):
            self.assertEqual(ir._respell_devanagari(text), text)
        # in mixed text only the Devanagari word the rules know is respelled
        self.assertEqual(ir._respell_devanagari("Chrome खोलो and वोल्यूम कम करो"), "Chrome खोलो and वॉल्यूम कम करो")

    def test_words_the_rules_dont_know_are_left_alone(self):
        self.assertEqual(ir._respell_devanagari("दूध लाना याद रखना"), "दूध लाना याद रखना")
        self.assertEqual(ir._respell_devanagari("पीसी लॉक करो"), "पीसी लॉक करो")        # known words: no join


class VocabularyTests(unittest.TestCase):
    def tearDown(self):
        ir._devanagari_vocabulary.cache_clear()

    def test_read_off_the_rules_with_no_two_spellings_sharing_a_key(self):
        vocab, spellings = ir._devanagari_vocabulary()
        for word in ("वॉल्यूम", "रीलोड", "आवाज़", "बढ़ाओ", "हाइबरनेट", "खोलो"):
            self.assertIn(word, spellings)
        self.assertEqual(len(vocab), len({ir._deva_key(w) for w in spellings}))

    def test_a_phrase_added_to_the_rules_later_is_covered_too(self):
        extra = ir._Rule("tier56_probe", ir._contains("tier56_probe", "फ़ाइल ढूँढो"))
        ir._devanagari_vocabulary.cache_clear()
        with mock.patch.object(ir, "_RULES", [extra] + list(ir._RULES)):
            self.assertEqual(_skill("फाइल ढूंढो"), "tier56_probe")         # no nukta, anusvara for chandrabindu
        ir._devanagari_vocabulary.cache_clear()
        self.assertIsNone(_skill("फाइल ढूंढो"))


if __name__ == "__main__":
    unittest.main()
