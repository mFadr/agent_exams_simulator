"""Numerical (tolerance + units) and symbolic (SymPy) grading."""

from __future__ import annotations

import math
import random
import re
from typing import Any

import sympy
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

from ..exams.question_models import Question, ScoringRules
from .models import GradingMethod, QuestionResult, ResultStatus, status_for


class MathParseError(ValueError):
    pass


_ALLOWED_CHARS = re.compile(r"^[0-9A-Za-z_+\-*/^().,=\s!π√×÷·]*$")
_FUNCS: dict[str, Any] = {
    "sin": sympy.sin, "cos": sympy.cos, "tan": sympy.tan, "cot": sympy.cot,
    "asin": sympy.asin, "acos": sympy.acos, "atan": sympy.atan,
    "arcsin": sympy.asin, "arccos": sympy.acos, "arctan": sympy.atan,
    "sinh": sympy.sinh, "cosh": sympy.cosh, "tanh": sympy.tanh,
    "sqrt": sympy.sqrt, "exp": sympy.exp, "log": sympy.log, "ln": sympy.log,
    "abs": sympy.Abs, "Abs": sympy.Abs, "factorial": sympy.factorial,
    "pi": sympy.pi, "e": sympy.E, "E": sympy.E, "oo": sympy.oo, "I": sympy.I,
    "Integer": sympy.Integer, "Float": sympy.Float, "Rational": sympy.Rational, "Symbol": sympy.Symbol,
    "Function": sympy.Function,
}
_TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)


def parse_math_expression(text: str) -> sympy.Expr:
    """Safely parse a user/AI-provided math expression into SymPy.

    Input is restricted to a whitelist of characters, function names and short
    variable names before being handed to SymPy, so arbitrary Python cannot run.
    """
    s = (text or "").strip()
    if not s:
        raise MathParseError("empty expression")
    if len(s) > 400:
        raise MathParseError("expression too long")
    s = s.replace("π", "pi").replace("√", "sqrt").replace("×", "*").replace("÷", "/").replace("·", "*")
    if not _ALLOWED_CHARS.match(s) or "__" in s or "lambda" in s:
        raise MathParseError("expression contains unsupported characters")
    if "=" in s:  # "x = 3" -> take right-hand side
        s = s.split("=")[-1].strip()
        if not s:
            raise MathParseError("nothing after '='")
    # decimal comma (e.g. 9,81) when it cannot be an argument separator
    if "," in s and "(" not in s:
        s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    names = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", s))
    local: dict[str, Any] = {}
    for name in names:
        if name in _FUNCS:
            local[name] = _FUNCS[name]
        elif len(name) <= 3 and name.isalnum():
            local[name] = sympy.Symbol(name)
        else:
            raise MathParseError(f"unknown name '{name}'")
    try:
        expr = parse_expr(s, local_dict=local, global_dict={"__builtins__": {}, **_FUNCS}, transformations=_TRANSFORMS, evaluate=True)
    except Exception as exc:  # SymPy raises many exception types for bad syntax
        raise MathParseError(f"could not parse '{text}'") from exc
    if not isinstance(expr, sympy.Basic):
        raise MathParseError("not a mathematical expression")
    return expr


def expressions_equivalent(a: str | sympy.Expr, b: str | sympy.Expr, *, tol: float = 1e-9) -> bool:
    ea = parse_math_expression(a) if isinstance(a, str) else a
    eb = parse_math_expression(b) if isinstance(b, str) else b
    try:
        diff = sympy.simplify(ea - eb)
        if diff == 0:
            return True
    except Exception:
        pass
    symbols = sorted((ea.free_symbols | eb.free_symbols), key=str)
    rng = random.Random(1234)
    checks = 0
    for _ in range(12):
        subs = {s: rng.uniform(0.3, 2.7) for s in symbols}
        try:
            va = complex(ea.evalf(subs=subs))
            vb = complex(eb.evalf(subs=subs))
        except Exception:
            continue
        if any(map(math.isnan, (va.real, vb.real))):
            continue
        checks += 1
        if abs(va - vb) > tol * max(1.0, abs(vb)):
            return False
        if not symbols:
            break
    return checks > 0


# --------------------------------------------------------------------------- units

_PREFIX = {"G": 1e9, "M": 1e6, "k": 1e3, "h": 1e2, "c": 1e-2, "m": 1e-3, "µ": 1e-6, "u": 1e-6, "n": 1e-9}
_UNIT_ALIASES = {
    "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "minute": "min", "minutes": "min", "mins": "min",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg",
    "liter": "L", "litre": "L", "l": "L", "liters": "L", "litres": "L",
    "deg": "°", "degree": "°", "degrees": "°",
    "percent": "%", "ohm": "Ω", "ohms": "Ω",
}


def normalize_unit(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip().replace(" ", "").replace("·", "*").replace("^", "").replace("**", "")
    return _UNIT_ALIASES.get(u, _UNIT_ALIASES.get(u.lower(), u))


def unit_factor(given: str, expected: str) -> float | None:
    """Factor f such that value_in_given * f = value_in_expected, or None if incompatible."""
    g, e = normalize_unit(given), normalize_unit(expected)
    if g == e:
        return 1.0
    if g.lower() == e.lower() and len(g) > 2:  # e.g. "Pa" vs "pa" for longer names
        return 1.0
    # simple single-prefix conversions (km vs m, mg vs g, ...)
    if len(g) > 1 and g[0] in _PREFIX and g[1:] == e:
        return _PREFIX[g[0]]
    if len(e) > 1 and e[0] in _PREFIX and e[1:] == g:
        return 1.0 / _PREFIX[e[0]]
    if len(g) > 1 and len(e) > 1 and g[0] in _PREFIX and e[0] in _PREFIX and g[1:] == e[1:]:
        return _PREFIX[g[0]] / _PREFIX[e[0]]
    time = {"s": 1.0, "min": 60.0, "h": 3600.0}
    if g in time and e in time:
        return time[g] / time[e]
    return None


_NUM_UNIT = re.compile(r"^(?P<num>.*?[0-9)\]πe])\s*(?P<unit>[A-Za-zµΩ°%][A-Za-z0-9µΩ°%/^*·\-]*)?\s*$")


def try_parse_number(text: str) -> float | None:
    try:
        value, _ = parse_number_with_unit(str(text))
        return value
    except MathParseError:
        return None


def parse_number_with_unit(text: str) -> tuple[float, str | None]:
    """Parse '9.81', '9,81 m/s^2', '1/3', '2*sqrt(2) cm', '1.2e3 J' into (value, unit)."""
    s = (text or "").strip()
    if not s:
        raise MathParseError("empty answer")
    s = s.replace("−", "-").replace(" ", " ")
    # 1) pure expression (also handles 'pi', 'sqrt(2)')
    try:
        expr = parse_math_expression(s)
        if not expr.free_symbols:
            return float(sympy.N(expr)), None
    except (MathParseError, TypeError, ValueError):
        pass
    # 2) number/expression followed by a unit
    m = _NUM_UNIT.match(s)
    if m and m.group("unit"):
        num, unit = m.group("num").strip(), m.group("unit")
        try:
            expr = parse_math_expression(num)
            if not expr.free_symbols:
                return float(sympy.N(expr)), unit
        except (MathParseError, TypeError, ValueError):
            pass
        # maybe the split was wrong, e.g. "3 m/s" with num "3"
    parts = s.split(None, 1)
    if len(parts) == 2:
        try:
            expr = parse_math_expression(parts[0])
            if not expr.free_symbols:
                return float(sympy.N(expr)), parts[1]
        except (MathParseError, TypeError, ValueError):
            pass
    raise MathParseError(f"'{text}' is not a number")


def within_tolerance(value: float, expected: float, abs_tol: float, rel_tol: float) -> bool:
    if math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-12):
        return True
    allowed = max(abs_tol, rel_tol * abs(expected))
    return abs(value - expected) <= allowed


def diagnose_numeric_error(value: float, expected: float) -> list[str]:
    tags: list[str] = []
    if expected == 0 or value == 0:
        return tags
    if math.isclose(-value, expected, rel_tol=0.02):
        tags.append("sign_error")
    ratio = abs(value / expected)
    if ratio > 0:
        lg = math.log10(ratio)
        if abs(lg - round(lg)) < 0.01 and round(lg) != 0:
            tags.append("order_of_magnitude")
    if not tags and abs(value - expected) <= 0.05 * abs(expected):
        tags.append("rounding_or_precision")
    if not tags:
        tags.append("calculation_error")
    return tags


_TAG_TEXT = {
    "sign_error": "The magnitude is right but the sign is wrong.",
    "order_of_magnitude": "Off by a power of ten - check unit conversions or decimal placement.",
    "rounding_or_precision": "Close to the expected value but outside the tolerance - check rounding of intermediate results.",
    "calculation_error": "The value differs from the expected result.",
    "missing_unit": "The unit was missing.",
    "wrong_unit": "The unit is incompatible with the expected unit.",
}


def grade_numerical(q: Question, value: Any, rules: ScoringRules) -> QuestionResult:
    n = q.numerical
    assert n is not None
    try:
        given, unit = parse_number_with_unit(str(value))
    except MathParseError:
        return QuestionResult(
            question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.INCORRECT,
            method=GradingMethod.NUMERICAL, feedback=f"Could not read '{value}' as a number.",
            error_tags=["unreadable_number"],
        )
    tags: list[str] = []
    notes: list[str] = []
    factor = 1.0
    if n.unit:
        if not unit:
            tags.append("missing_unit")
        else:
            f = unit_factor(unit, n.unit)
            if f is None:
                tags.append("wrong_unit")
                factor = 1.0
            else:
                factor = f
                if f != 1.0:
                    notes.append(f"Converted {unit} → {n.unit}.")
    converted = given * factor
    if within_tolerance(converted, n.value, n.abs_tolerance, n.rel_tolerance):
        if "wrong_unit" in tags:
            points = 0.5 * q.max_points if rules.partial_credit else 0.0
            fb = f"The number is right but the unit '{unit}' is not compatible with '{n.unit}'."
        elif "missing_unit" in tags:
            points = 0.8 * q.max_points if rules.partial_credit else q.max_points
            fb = f"Correct value, but the unit ({n.unit}) was not stated."
        else:
            points = q.max_points
            fb = "Correct (within tolerance)."
        return QuestionResult(
            question_id=q.id, points=points, max_points=q.max_points, status=status_for(points, q.max_points),
            method=GradingMethod.NUMERICAL, feedback=" ".join([fb, *notes]), error_tags=tags,
        )
    tags.extend(diagnose_numeric_error(converted, n.value))
    fb = " ".join(_TAG_TEXT.get(t, t) for t in tags)
    penalty = -rules.negative_fraction * q.max_points if rules.negative_marking else 0.0
    return QuestionResult(
        question_id=q.id, points=penalty, max_points=q.max_points, status=ResultStatus.INCORRECT,
        method=GradingMethod.NUMERICAL, feedback=f"Incorrect: {given:g}{' ' + unit if unit else ''}. {fb}".strip(),
        error_tags=tags,
    )


def extract_final_answer(text: str) -> str:
    """Take the final answer from a worked solution: last non-empty line, right of the last '='."""
    lines = [ln.strip() for ln in str(text).strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    for prefix in ("answer:", "final answer:", "ans:", "result:"):
        if last.lower().startswith(prefix):
            last = last[len(prefix):].strip()
    if "=" in last:
        last = last.split("=")[-1].strip()
    return last.rstrip(".")


def grade_symbolic(q: Question, value: Any) -> QuestionResult | None:
    """Check the final answer of a math problem for symbolic equivalence.

    Returns ``None`` if the answer cannot be parsed (caller may fall back to a rubric).
    """
    if not q.expected_expression:
        return None
    final = extract_final_answer(str(value))
    try:
        ok = expressions_equivalent(final, q.expected_expression)
    except MathParseError:
        return None
    if ok:
        return QuestionResult(
            question_id=q.id, points=q.max_points, max_points=q.max_points, status=ResultStatus.CORRECT,
            method=GradingMethod.SYMBOLIC, feedback=f"Final answer '{final}' is mathematically equivalent to the expected result.",
        )
    return QuestionResult(
        question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.INCORRECT,
        method=GradingMethod.SYMBOLIC, feedback=f"Final answer '{final}' is not equivalent to the expected result.",
        error_tags=["wrong_final_answer"],
    )
