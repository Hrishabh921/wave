"""
automation/calculator.py — Tier 16: "what's 340 minus 15% of it", "convert
12 miles to km".

Gap analysis: "Calculator: safe AST evaluation (orchestrator/calc_utils.py).
Wave leaves arithmetic to the LLM." An LLM doing its own mental arithmetic
is exactly the kind of thing that looks right and occasionally isn't, with
no way for the user to tell which answer they got - this gives it a real
calculator to call instead. Two independent pieces:

calculate(expression)   - evaluates a numeric expression via Python's own
                           `ast` module, walking a small node WHITELIST
                           (numbers, +-*/ //, %, **, unary +-) - never
                           through eval()/exec() on the raw string, which
                           would let arbitrary code run. Any node outside
                           that whitelist (a name, a call, an attribute, a
                           subscript, ...) is rejected by construction,
                           the same default-deny shape brain/content_trust.py's
                           module docstring describes for that module's own
                           boundary: allow a short list of known-safe
                           things, reject everything else, rather than
                           trying to blacklist specific dangerous ones.
                           "15% of 340"-style phrasing is normalized to
                           plain arithmetic before parsing (see
                           _normalize_percent_phrasing) since it's common
                           enough to be worth a direct rewrite; anything
                           else informal ("340 divided by 7", "340 x 2")
                           is left to the LLM to turn into clean notation
                           before it calls this tool - exactly the kind of
                           slot-filling brain/handlers/reminders.py's own
                           docstring already notes an LLM does reliably
                           and a hand-rolled parser does not.

convert_units(value, from_unit, to_unit)
                         - a small, explicit conversion table (length,
                           mass, volume, temperature). Deliberately not
                           the `pint` package - not a dependency anywhere
                           else in this project, and pulling it in for a
                           feature this small would cut against the "keep
                           the base install lean" ground rule.

Both are plain functions, not a manager class - there's no storage and no
state to hold between calls, so a class would just be ceremony.
"""
import ast
import logging
import operator
import re

logger = logging.getLogger("Calculator")

# ---------------------------------------------------------------------------
# calculate()
# ---------------------------------------------------------------------------
_MAX_EXPR_LEN = 200       # a pasted essay isn't a calculation; fail fast and cheaply
_MAX_POWER_EXPONENT = 1000  # guards a**b from hanging/exhausting memory on a huge b

_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_PERCENT_OF_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s+of\s+(\d+(?:\.\d+)?)", re.I)
# 🔧 Any '%' NOT immediately followed by (optional whitespace, then) a digit
# is a PERCENT sign ('50%', '340 - 15%') and becomes '/100'. One still
# followed by a digit - '10 % 4', '10%4' - reads as Python's modulo
# operator instead and is left alone for _eval_node's ast.Mod branch to
# handle. This is the disambiguation _normalize_percent_phrasing's own
# docstring below describes; an earlier version of this regex didn't
# exist and a blanket expr.replace("%", "/100") ran unconditionally,
# which silently turned "10 % 4" into the invalid "10 /100 4" and made
# _ALLOWED_BINOPS' own ast.Mod entry unreachable - caught by
# tests/test_tier16_calculator.py before this ever shipped.
_BARE_PERCENT_RE = re.compile(r"%(?!\s*\d)")


def _normalize_percent_phrasing(expr: str) -> str:
    """'15% of 340' -> '(15/100)*340'. Any OTHER '%' is read as a percent
    sign ('/100') UNLESS a number follows it ('10 % 4', '10%4'), which
    reads as Python's modulo operator instead and is left untouched for
    ast.Mod to handle - see _BARE_PERCENT_RE above for the exact rule."""
    expr = _PERCENT_OF_RE.sub(lambda m: f"({m.group(1)}/100)*{m.group(2)}", expr)
    return _BARE_PERCENT_RE.sub("/100", expr)


def _eval_node(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("only plain numbers are allowed")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"'{type(node.op).__name__}' isn't a supported operator")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POWER_EXPONENT:
            raise ValueError("that exponent is too large for me to calculate")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"'{type(node.op).__name__}' isn't a supported operator")
        return op(_eval_node(node.operand))
    raise ValueError(f"'{type(node).__name__}' isn't something I can evaluate")


def _tidy_number(n):
    if isinstance(n, float) and n.is_integer() and abs(n) < 1e15:
        return int(n)
    return n


def calculate(expression: str) -> str:
    raw = (expression or "").strip()
    if not raw:
        return "What would you like me to calculate?"
    if len(raw) > _MAX_EXPR_LEN:
        return "That expression is too long for me to calculate safely."
    try:
        tree = ast.parse(_normalize_percent_phrasing(raw), mode="eval")
        result = _eval_node(tree.body)
    except ZeroDivisionError:
        return f"Can't calculate '{raw}' - that divides by zero."
    except (SyntaxError, ValueError, TypeError, OverflowError, RecursionError) as e:
        return f"'{raw}' isn't a calculation I can evaluate ({e})."
    return f"{raw} = {_tidy_number(result)}"


# ---------------------------------------------------------------------------
# convert_units()
# ---------------------------------------------------------------------------
_LENGTH_TO_METERS = {
    "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001, "millimetre": 0.001, "millimetres": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01, "centimetre": 0.01, "centimetres": 0.01,
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0, "kilometre": 1000.0, "kilometres": 1000.0,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
}
_MASS_TO_GRAMS = {
    "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.349523125, "ounce": 28.349523125, "ounces": 28.349523125,
    "lb": 453.59237, "lbs": 453.59237, "pound": 453.59237, "pounds": 453.59237,
}
_VOLUME_TO_LITERS = {
    "ml": 0.001, "milliliter": 0.001, "milliliters": 0.001, "millilitre": 0.001, "millilitres": 0.001,
    "l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0,
    "tsp": 0.00492892, "teaspoon": 0.00492892, "teaspoons": 0.00492892,
    "tbsp": 0.0147868, "tablespoon": 0.0147868, "tablespoons": 0.0147868,
    "cup": 0.236588, "cups": 0.236588,
    "pt": 0.473176, "pint": 0.473176, "pints": 0.473176,
    "qt": 0.946353, "quart": 0.946353, "quarts": 0.946353,
    "gal": 3.78541, "gallon": 3.78541, "gallons": 3.78541,
}
_CATEGORIES = {"length": _LENGTH_TO_METERS, "mass": _MASS_TO_GRAMS, "volume": _VOLUME_TO_LITERS}
_TEMP_UNITS = {"c": "c", "celsius": "c", "centigrade": "c",
               "f": "f", "fahrenheit": "f",
               "k": "k", "kelvin": "k"}
_TEMP_LABEL = {"c": "°C", "f": "°F", "k": "K"}


def _normalize_unit(u: str) -> str:
    return (u or "").strip().lower().rstrip(".")


def _find_category(unit: str):
    for name, table in _CATEGORIES.items():
        if unit in table:
            return name, table
    return None, None


def _convert_temperature(value, from_key, to_key):
    """None if this isn't a temperature pair at all (both from_key AND
    to_key have to be recognized temperature units) - temperature is an
    AFFINE conversion (a scale AND an offset), unlike every other category
    here, which is a plain multiply against a shared base unit, so it
    can't live in the same ratio-table shape as _CATEGORIES."""
    f, t = _TEMP_UNITS.get(from_key), _TEMP_UNITS.get(to_key)
    if f is None or t is None:
        return None
    if f == "c":
        celsius = value
    elif f == "f":
        celsius = (value - 32) * 5.0 / 9.0
    else:
        celsius = value - 273.15
    if t == "c":
        out = celsius
    elif t == "f":
        out = celsius * 9.0 / 5.0 + 32
    else:
        out = celsius + 273.15
    return f"{value}{_TEMP_LABEL[f]} = {_tidy_number(round(out, 2))}{_TEMP_LABEL[t]}"


def convert_value(value: float, from_unit: str, to_unit: str):
    """Tier 48 (brain/exact_answers.py): the converted number, or None when
    a unit isn't known or the two aren't the same kind."""
    f, t = _normalize_unit(from_unit), _normalize_unit(to_unit)
    tf, tt = _TEMP_UNITS.get(f), _TEMP_UNITS.get(t)
    if tf and tt:
        celsius = value if tf == "c" else (value - 32) * 5.0 / 9.0 if tf == "f" else value - 273.15
        return celsius if tt == "c" else celsius * 9.0 / 5.0 + 32 if tt == "f" else celsius + 273.15
    cat_from, table_from = _find_category(f)
    cat_to, table_to = _find_category(t)
    if cat_from is None or cat_from != cat_to:
        return None
    return value * table_from[f] / table_to[t]


def unit_names() -> list:
    """Every unit word convert_value() knows."""
    return sorted({u for table in _CATEGORIES.values() for u in table} | set(_TEMP_UNITS), key=len, reverse=True)


def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    from_key, to_key = _normalize_unit(from_unit), _normalize_unit(to_unit)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return f"'{value}' isn't a number I can convert."

    temp_result = _convert_temperature(value, from_key, to_key)
    if temp_result is not None:
        return temp_result

    cat_from, table_from = _find_category(from_key)
    cat_to, table_to = _find_category(to_key)
    if cat_from is None:
        return f"I don't know the unit '{from_unit}'."
    if cat_to is None:
        return f"I don't know the unit '{to_unit}'."
    if cat_from != cat_to:
        return f"'{from_unit}' and '{to_unit}' aren't the same kind of unit ({cat_from} vs {cat_to})."

    converted = (value * table_from[from_key]) / table_to[to_key]
    return f"{value} {from_unit} = {_tidy_number(round(converted, 6))} {to_unit}"
