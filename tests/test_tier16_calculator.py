"""
tests/test_tier16_calculator.py — Tier 16: automation/calculator.py's
calculate() (safe AST evaluation - never eval()/exec() on the raw string)
and convert_units() (length/mass/volume/temperature), plus the
brain/handlers/productivity.py skill wrappers around both.

Run: python -m unittest discover -s tests -p "test_tier16_calculator.py"
(pytest will also collect it.)
"""
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation.calculator import calculate, convert_units          # noqa: E402
from brain.skills_registry import skills                            # noqa: E402
import brain.handlers  # noqa: E402,F401  (side effect: registers every built-in skill)


class CalculateTests(unittest.TestCase):
    def test_basic_arithmetic(self):
        self.assertEqual(calculate("2 + 2"), "2 + 2 = 4")
        self.assertEqual(calculate("10 - 3"), "10 - 3 = 7")
        self.assertEqual(calculate("4 * 5"), "4 * 5 = 20")
        self.assertEqual(calculate("10 / 4"), "10 / 4 = 2.5")
        self.assertEqual(calculate("10 // 4"), "10 // 4 = 2")
        self.assertEqual(calculate("10 % 4"), "10 % 4 = 2")
        self.assertEqual(calculate("2 ** 10"), "2 ** 10 = 1024")

    def test_operator_precedence_and_parens(self):
        self.assertEqual(calculate("(4 + 5) * 3"), "(4 + 5) * 3 = 27")
        self.assertEqual(calculate("4 + 5 * 3"), "4 + 5 * 3 = 19")

    def test_unary_operators(self):
        self.assertEqual(calculate("-5 + 3"), "-5 + 3 = -2")
        self.assertEqual(calculate("+5"), "+5 = 5")

    def test_float_result_that_is_a_whole_number_is_tidied_to_an_int(self):
        self.assertEqual(calculate("4 / 2"), "4 / 2 = 2")  # not "2.0"

    def test_float_result_that_is_not_whole_stays_a_float(self):
        self.assertEqual(calculate("1 / 3"), "1 / 3 = 0.3333333333333333")

    def test_percent_of_phrasing_is_normalized(self):
        self.assertEqual(calculate("15% of 340"), "15% of 340 = 51")
        self.assertEqual(calculate("340 - 15% of 340"), "340 - 15% of 340 = 289")

    def test_bare_percent_divides_by_100(self):
        self.assertEqual(calculate("50%"), "50% = 0.5")

    def test_percent_sign_used_as_modulo_still_works(self):
        # Regression: an earlier _normalize_percent_phrasing unconditionally
        # replaced every '%' with '/100', which turned "10 % 4" into the
        # invalid "10 /100 4" and made _ALLOWED_BINOPS' ast.Mod entry
        # unreachable. A '%' immediately followed by a number (with or
        # without a space) is modulo, not percent.
        self.assertEqual(calculate("10 % 4"), "10 % 4 = 2")
        self.assertEqual(calculate("10%4"), "10%4 = 2")

    def test_empty_expression(self):
        self.assertEqual(calculate(""), "What would you like me to calculate?")
        self.assertEqual(calculate("   "), "What would you like me to calculate?")

    def test_division_by_zero_is_caught_cleanly(self):
        result = calculate("1 / 0")
        self.assertIn("divides by zero", result)

    def test_floor_division_by_zero_is_also_caught(self):
        result = calculate("1 // 0")
        self.assertIn("divides by zero", result)

    def test_too_long_expression_is_rejected_before_parsing(self):
        result = calculate("1" + "+1" * 150)
        self.assertIn("too long", result)

    def test_huge_exponent_is_rejected_rather_than_hanging(self):
        result = calculate("9 ** 9999999999")
        self.assertIn("too large", result)

    def test_exponent_bomb_chain_short_circuits_before_any_huge_compute(self):
        # Each ** is evaluated right-to-left; 2**2**2**2**2**2 would already
        # be astronomically large by the time it reached the outermost **
        # if the guard didn't catch an oversized exponent on the way up.
        result = calculate("2**2**2**2**2**2**2**2**2")
        self.assertIn("too large", result)

    def test_bool_literals_are_rejected_despite_being_an_int_subtype(self):
        # isinstance(True, int) is True in Python - calculate() explicitly
        # excludes bool so "True + 1"-style expressions can't sneak through.
        result = calculate("True + 1")
        self.assertIn("isn't a calculation I can evaluate", result)

    def test_names_calls_and_attributes_are_all_rejected_not_executed(self):
        # The whole point of the AST whitelist: none of these should ever
        # reach Python's own eval()/exec() on the raw string.
        for expr in ("import os", "__import__('os').system('echo hi')",
                     "open('/etc/passwd').read()", "[].__class__",
                     "1 if True else 2", "x", "(1).bit_length()"):
            result = calculate(expr)
            self.assertIn("isn't a calculation I can evaluate", result, msg=f"expr={expr!r}")

    def test_malformed_syntax_is_reported_not_raised(self):
        result = calculate("2 + + + ")
        self.assertIn("isn't a calculation I can evaluate", result)


class ConvertUnitsTests(unittest.TestCase):
    def test_length(self):
        self.assertEqual(convert_units(12, "miles", "km"), "12.0 miles = 19.312128 km")
        self.assertEqual(convert_units(1, "m", "cm"), "1.0 m = 100 cm")

    def test_mass(self):
        self.assertEqual(convert_units(1, "kg", "lb"), "1.0 kg = 2.204623 lb")

    def test_volume(self):
        self.assertEqual(convert_units(2, "cups", "ml"), "2.0 cups = 473.176 ml")

    def test_temperature_fahrenheit_to_celsius(self):
        self.assertEqual(convert_units(100, "F", "C"), "100.0°F = 37.78°C")

    def test_temperature_celsius_to_kelvin(self):
        self.assertEqual(convert_units(0, "C", "K"), "0.0°C = 273.15K")

    def test_temperature_is_affine_not_a_ratio_check(self):
        # 0 degrees is not "nothing" in F/C, unlike every ratio-based unit -
        # this is the whole reason temperature can't share _CATEGORIES' table.
        result = convert_units(0, "F", "C")
        self.assertIn("-17.78", result)

    def test_unknown_from_unit(self):
        result = convert_units(5, "smoots", "m")
        self.assertIn("don't know the unit 'smoots'", result)

    def test_unknown_to_unit(self):
        result = convert_units(5, "m", "smoots")
        self.assertIn("don't know the unit 'smoots'", result)

    def test_mismatched_categories_are_rejected(self):
        result = convert_units(5, "kg", "m")
        self.assertIn("aren't the same kind of unit", result)
        self.assertIn("mass", result)
        self.assertIn("length", result)

    def test_non_numeric_value_is_reported_not_raised(self):
        result = convert_units("not a number", "kg", "lb")
        self.assertIn("isn't a number I can convert", result)

    def test_case_and_trailing_period_insensitive_units(self):
        # The output message echoes the units AS GIVEN (so "KG"/"lb." come
        # back verbatim, not normalized) - only the lookup itself is
        # case/period-insensitive, so compare the converted NUMBER, not
        # the two calls' full strings.
        self.assertIn("2.204623", convert_units(1, "KG", "lb."))
        self.assertIn("2.204623", convert_units(1, "kg", "lb"))


class CalculatorSkillWrapperTests(unittest.TestCase):
    """Thin pass-through checks - calculate()/convert_units()'s own
    correctness is covered above; this just pins the argument wiring."""
    def _call(self, name, **kwargs):
        return skills.get_handler(name)(None, kwargs)

    def test_calculate_skill(self):
        self.assertEqual(self._call("calculate", expression="2+2"), "2+2 = 4")

    def test_convert_units_skill(self):
        result = self._call("convert_units", value=12, from_unit="miles", to_unit="km")
        self.assertIn("19.312128 km", result)


if __name__ == "__main__":
    unittest.main()
