"""
Tier 13 - which language the user is speaking (voice/language.py) and text
made ready for a voice in that language (voice/text_prep.py).

Run with:  python -m pytest tests/test_tier13_language_text.py
"""
import unittest

import _phase4_support as S  # noqa: F401

from voice import language as L
from voice import text_prep as T


class ClassifyTests(unittest.TestCase):
    CASES = [
        ("आज मौसम कैसा है", L.HI),
        ("मुझे कल सुबह याद दिलाना", L.HI),
        ("क्रोम खोलो please", L.HI),
        ("volume badhao", L.HINGLISH),
        ("chrome kholo", L.HINGLISH),
        ("aaj mausam kaisa hai", L.HINGLISH),
        ("mujhe kal subah 9 baje yaad dilana", L.HINGLISH),
        ("bhai ek gaana chalao", L.HINGLISH),
        ("main ghar ja raha hoon", L.HINGLISH),
        ("what is the weather today", L.EN),
        ("open chrome", L.EN),
        ("close the tab", L.EN),
        ("I want to go to the park, is it open?", L.EN),
        ("The main thing is to do it", L.EN),
        ("I am a teen", L.EN),
        ("Turned the volume up.", L.EN),
        ("", L.EN),
    ]

    def test_examples(self):
        wrong = [(t, L.classify(t), want) for t, want in self.CASES if L.classify(t) != want]
        self.assertEqual(wrong, [])

    def test_english_words_that_are_also_hindi_dont_make_english_hinglish(self):
        for text in ("do it to me", "is the band on par", "main menu", "the hum of the fan"):
            self.assertEqual(L.classify(text), L.EN, text)

    def test_ratios(self):
        self.assertEqual(L.devanagari_ratio("abc"), 0.0)
        self.assertEqual(L.devanagari_ratio("नमस्ते"), 1.0)
        self.assertGreater(L.hinglish_ratio("kya haal hai"), 0.5)
        self.assertTrue(L.has_arabic_script("آج موسم کیسا ہے"))
        self.assertFalse(L.has_arabic_script("aaj mausam"))


class TrackerTests(unittest.TestCase):
    def test_short_ambiguous_utterances_keep_the_language(self):
        tr = L.LanguageTracker()
        self.assertEqual(tr.observe("aaj mausam kaisa hai"), L.HINGLISH)
        for text in ("ok", "haan", "stop", "5", "open chrome"):
            self.assertEqual(tr.observe(text), L.HINGLISH, text)
        self.assertEqual(tr.observe("what is on my calendar today"), L.EN)
        self.assertEqual(tr.observe("आज मौसम कैसा है"), L.HI)

    def test_what_the_recognizers_are_told(self):
        tr = L.LanguageTracker()
        self.assertIsNone(tr.whisper_language(), "nothing heard yet: let Whisper detect")
        self.assertEqual(tr.google_language("en-IN"), "en-IN")
        tr.observe("मुझे कल याद दिलाना")
        self.assertEqual((tr.whisper_language(), tr.google_language("en-IN")), ("hi", "hi-IN"))
        self.assertIn("ठीक", tr.initial_prompt())
        tr.observe("volume badhao yaar")
        self.assertEqual(tr.whisper_language(), "en", "Hinglish decodes as English so it stays in Latin letters")
        self.assertIn("kholo", tr.initial_prompt())
        tr.observe("set a timer for ten minutes")
        self.assertIsNone(tr.initial_prompt())
        self.assertEqual(tr.google_language("hi-IN"), "en-IN")

    def test_whisper_hearing_hindi_in_latin_letters_is_hinglish(self):
        tr = L.LanguageTracker()
        self.assertEqual(tr.observe("theek hai chalo", detected="hi", probability=0.95), L.HINGLISH)

    def test_a_confident_detection_on_a_short_word_does_not_flip_it(self):
        tr = L.LanguageTracker()
        tr.observe("aaj mausam kaisa hai")
        self.assertEqual(tr.observe("ok", detected="en", probability=0.99), L.HINGLISH)
        self.assertEqual(tr.observe("please read me the news", detected="en", probability=0.99), L.EN)

    def test_reset(self):
        tr = L.LanguageTracker()
        tr.observe("आज मौसम कैसा है")
        tr.reset()
        self.assertEqual(tr.current, L.EN)
        self.assertIsNone(tr.whisper_language())


class NumberWordTests(unittest.TestCase):
    def test_english(self):
        self.assertEqual(T.number_to_words_en(0), "zero")
        self.assertEqual(T.number_to_words_en(45), "forty-five")
        self.assertEqual(T.number_to_words_en(101), "one hundred one")
        self.assertEqual(T.number_to_words_en(1234567),
                         "one million two hundred thirty-four thousand five hundred sixty-seven")
        self.assertEqual(T.number_to_words_en(150000, indian=True), "one lakh fifty thousand")
        self.assertEqual(T.number_to_words_en(25000000, indian=True), "two crore fifty lakh")
        self.assertEqual(T.number_to_words_en(-7), "minus seven")

    def test_english_ordinals_and_years(self):
        self.assertEqual([T.ordinal_en(n) for n in (1, 2, 3, 4, 5, 12, 20, 22, 40, 101)],
                         ["first", "second", "third", "fourth", "fifth", "twelfth", "twentieth", "twenty-second",
                          "fortieth", "one hundred first"])
        self.assertEqual([T.year_en(y) for y in (1990, 2000, 2005, 2026, 1905, 1900)],
                         ["nineteen ninety", "two thousand", "two thousand five", "twenty twenty-six",
                          "nineteen oh five", "nineteen hundred"])

    def test_hindi(self):
        self.assertEqual(len(T._HI_0_99), 100)
        self.assertEqual([T.number_to_words_hi(n) for n in (0, 5, 11, 25, 49, 99)],
                         ["शून्य", "पाँच", "ग्यारह", "पच्चीस", "उनचास", "निन्यानवे"])
        self.assertEqual(T.number_to_words_hi(101), "एक सौ एक")
        self.assertEqual(T.number_to_words_hi(1500), "एक हज़ार पाँच सौ")
        self.assertEqual(T.number_to_words_hi(150000), "एक लाख पचास हज़ार")
        self.assertEqual(T.number_to_words_hi(23400000), "दो करोड़ चौंतीस लाख")
        self.assertEqual([T.ordinal_hi(n) for n in (1, 2, 3, 4, 5, 6)],
                         ["पहला", "दूसरा", "तीसरा", "चौथा", "पाँचवाँ", "छठा"])


class PrepareTests(unittest.TestCase):
    def test_markup_urls_and_emoji(self):
        out = T.prepare("See https://github.com/x/y and [the docs](https://docs.example.com) 🎉", "en")
        self.assertEqual(out, "See a link and the docs")
        self.assertIn("एक लिंक", T.prepare("यह देखो www.example.com", "hi"))

    def test_abbreviations(self):
        self.assertEqual(T.prepare("e.g. Dr. Rao vs Mr. Shah", "en", numbers="none"),
                         "for example, Doctor Rao versus Mister Shah")

    def test_rupees_the_indian_way(self):
        self.assertEqual(T.prepare("Your balance is ₹1,50,000.", "en"), "Your balance is one lakh fifty thousand rupees.")
        self.assertEqual(T.prepare("₹1,50,000", "hi"), "एक लाख पचास हज़ार रुपये")
        self.assertEqual(T.prepare("Rs. 500", "en"), "five hundred rupees")
        self.assertEqual(T.prepare("₹2.5 lakh", "en"), "two point five lakh rupees")
        self.assertEqual(T.prepare("$20.50", "en"), "twenty dollars and fifty cents")

    def test_times(self):
        self.assertEqual(T.prepare("at 9:30 pm, then 10:05", "en"), "at nine thirty p.m., then ten oh five")
        self.assertEqual(T.prepare("it's 2:00", "en"), "it's two o'clock")
        self.assertEqual(T.prepare("मीटिंग 9:30 बजे है", "hi"), "मीटिंग साढ़े नौ बजे है")
        self.assertEqual(T.prepare("1:30", "hi"), "डेढ़ बजे")
        self.assertEqual(T.prepare("2:30 pm", "hi"), "दोपहर ढाई बजे")
        self.assertEqual(T.prepare("10:15", "hi"), "सवा दस बजे")
        self.assertEqual(T.prepare("3:45", "hi"), "पौने चार बजे")
        self.assertEqual(T.prepare("12:45", "hi"), "पौने एक बजे")
        self.assertEqual(T.prepare("9:05 बजे", "hi"), "नौ बजकर पाँच मिनट")
        self.assertEqual(T.prepare("kal 9:30 baje", "en"), "kal nine thirty baje")

    def test_dates_percent_temperature_units(self):
        self.assertEqual(T.prepare("2026-11-04", "en"), "November fourth, twenty twenty-six")
        self.assertEqual(T.prepare("04/11/2026", "hi"), "चार नवंबर दो हज़ार छब्बीस")
        self.assertEqual(T.prepare("45%", "en"), "forty-five percent")
        self.assertEqual(T.prepare("45%", "hi"), "पैंतालीस प्रतिशत")
        self.assertEqual(T.prepare("25°C", "en"), "twenty-five degrees Celsius")
        self.assertEqual(T.prepare("1.5 GB in 10 mins", "en"), "one point five gigabytes in ten minutes")
        self.assertEqual(T.prepare("born in 1990", "en"), "born in nineteen ninety")
        self.assertEqual(T.prepare("the 22nd", "en"), "the twenty-second")

    def test_risky_mode_leaves_what_cloud_voices_read_well(self):
        text = "At 9:30 pm it was 25°C and 45%; I paid $20 and ₹1,50,000 for 1,50,000 items."
        out = T.prepare(text, "en", numbers="risky")
        self.assertIn("9:30 pm", out)
        self.assertIn("25°C", out)
        self.assertIn("$20", out)
        self.assertIn("one lakh fifty thousand rupees", out)
        self.assertIn("for one lakh fifty thousand items", out)

    def test_a_hindi_voice_always_gets_hindi_numbers(self):
        self.assertEqual(T.prepare("आज 3 बजे", "hi", numbers="risky"), "आज तीन बजे")

    def test_none_leaves_numbers(self):
        self.assertEqual(T.prepare("₹1,50,000 at 9:30", "en", numbers="none"), "₹1,50,000 at 9:30")


if __name__ == "__main__":
    unittest.main()
