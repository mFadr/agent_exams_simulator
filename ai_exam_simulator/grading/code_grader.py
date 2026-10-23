"""Programming-question grading: static analysis and optional restricted execution.

Execution model (only when the user enables it in Application Settings):

* the student's code runs in a **separate Python process** started in isolated
  mode (``-I -S -B``) inside an empty temporary directory, with a stripped
  environment (no API keys, no user variables) and a hard wall-clock timeout;
* inside that process the code executes with a reduced set of builtins
  (no ``open``, ``exec``, ``eval``, ``input``...) and ``import`` is limited to a
  small whitelist of pure-computation modules;
* the process is killed when the timeout expires.

This is *not* an OS-level sandbox: Windows offers no simple, dependable
unprivileged isolation for Python code, so execution is **disabled by
default**. When disabled, answers are checked statically and graded by rubric
(AI or manual), and the limitation is stated in the result.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

from ..exams.question_models import Question
from .models import GradingMethod, QuestionResult, ResultStatus, status_for

RUNNER_SOURCE = r'''
import sys, json, ast
ALLOWED = {"math", "itertools", "functools", "collections", "re", "string", "heapq", "bisect",
           "statistics", "fractions", "decimal", "operator", "typing", "dataclasses", "copy"}
_real_import = __import__
def _limited_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in ALLOWED:
        raise ImportError("import of '%s' is not allowed in the exam sandbox" % name)
    return _real_import(name, globals, locals, fromlist, level)
import builtins as _b
SAFE = {k: getattr(_b, k) for k in (
    "abs all any bin bool bytes chr dict divmod enumerate filter float format frozenset hash hex int "
    "isinstance issubclass iter len list map max min next object oct ord pow print range repr reversed "
    "round set slice sorted str sum tuple type zip ValueError TypeError KeyError IndexError Exception "
    "StopIteration ZeroDivisionError ArithmeticError RuntimeError NotImplementedError AssertionError "
    "AttributeError property staticmethod classmethod super True False None callable getattr hasattr "
    "setattr id complex memoryview bytearray").split() if hasattr(_b, k)}
SAFE["__import__"] = _limited_import
SAFE["__build_class__"] = _b.__build_class__
SAFE["__name__"] = "__student__"
payload = json.loads(sys.stdin.read())
g = {"__builtins__": SAFE, "__name__": "__student__"}
out = {"compile_error": None, "results": []}
try:
    exec(compile(payload["code"], "<answer>", "exec"), g)
except BaseException as e:
    out["compile_error"] = "%s: %s" % (type(e).__name__, e)
    print(json.dumps(out)); sys.exit(0)
for t in payload["tests"]:
    r = {"call": t["call"], "ok": False, "got": None, "error": None}
    try:
        got = eval(t["call"], g)
        exp = ast.literal_eval(t["expected"])
        r["got"] = repr(got)[:200]
        r["ok"] = (got == exp) or (isinstance(got, float) and isinstance(exp, (int, float)) and abs(got - exp) <= 1e-9 * max(1, abs(exp)))
    except BaseException as e:
        r["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    out["results"].append(r)
print(json.dumps(out))
'''


@dataclass
class ExecutionOutcome:
    available: bool
    compile_error: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def find_python_interpreter() -> str | None:
    """Return a Python interpreter usable for running answers in a child process."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("py")


def static_check(code: str, function_name: str | None) -> tuple[bool, list[str]]:
    """Return (syntax_ok, notes)."""
    notes: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, [f"Syntax error on line {exc.lineno}: {exc.msg}"]
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if function_name and function_name not in funcs:
        notes.append(f"The required function '{function_name}' is not defined.")
    imports = {
        (a.name if isinstance(n, ast.Import) else (n.module or "")).split(".")[0]
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names
    }
    risky = imports & {"os", "sys", "subprocess", "socket", "shutil", "ctypes", "pathlib"}
    if risky:
        notes.append(f"Uses modules not allowed in the exam sandbox: {', '.join(sorted(risky))}.")
    if any(isinstance(n, ast.While) for n in ast.walk(tree)):
        notes.append("Contains a while-loop - make sure it terminates.")
    return True, notes


def run_tests_restricted(code: str, q: Question, timeout_s: int) -> ExecutionOutcome:
    python = find_python_interpreter()
    if not python:
        return ExecutionOutcome(available=False, error="No Python interpreter available for isolated execution.")
    payload = json.dumps({"code": code, "tests": [tc.model_dump() for tc in q.test_cases]})
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="aies_run_") as tmp:
        runner = os.path.join(tmp, "runner.py")
        with open(runner, "w", encoding="utf-8") as fh:
            fh.write(RUNNER_SOURCE)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        try:
            proc = subprocess.run(
                [python, "-I", "-S", "-B", runner], input=payload, capture_output=True, text=True,
                encoding="utf-8", timeout=timeout_s, cwd=tmp, env=env, creationflags=flags,
            )
        except subprocess.TimeoutExpired:
            return ExecutionOutcome(available=True, error=f"Execution exceeded the {timeout_s}s time limit (infinite loop?).")
        except OSError as exc:
            return ExecutionOutcome(available=False, error=f"Could not start isolated interpreter: {exc}")
    if proc.returncode != 0 or not proc.stdout.strip():
        return ExecutionOutcome(available=True, error=(proc.stderr or "Runner failed").strip()[-500:])
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return ExecutionOutcome(available=True, error="Could not read the test runner output.")
    return ExecutionOutcome(available=True, compile_error=data.get("compile_error"), results=data.get("results", []))


def grade_with_tests(q: Question, code: str, timeout_s: int) -> QuestionResult | None:
    """Run test cases; returns None if execution is not available."""
    outcome = run_tests_restricted(code, q, timeout_s)
    if not outcome.available:
        return None
    if outcome.error:
        return QuestionResult(
            question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.INCORRECT,
            method=GradingMethod.CODE_TESTS, feedback=outcome.error, error_tags=["runtime_error"],
        )
    if outcome.compile_error:
        return QuestionResult(
            question_id=q.id, points=0.0, max_points=q.max_points, status=ResultStatus.INCORRECT,
            method=GradingMethod.CODE_TESTS, feedback=f"Code could not run: {outcome.compile_error}",
            error_tags=["syntax_or_load_error"],
        )
    passed = sum(1 for r in outcome.results if r["ok"])
    total = len(outcome.results) or 1
    points = q.max_points * passed / total
    lines = []
    for r, tc in zip(outcome.results, q.test_cases):
        call = "(hidden test)" if tc.hidden else r["call"]
        if r["ok"]:
            lines.append(f"✔ {call}")
        else:
            detail = r["error"] or (f"got {r['got']}" if not tc.hidden else "wrong result")
            lines.append(f"✘ {call}: {detail}")
    tags = [] if passed == total else ["failed_tests"]
    if any(r.get("error") for r in outcome.results):
        tags.append("runtime_error")
    return QuestionResult(
        question_id=q.id, points=round(points, 4), max_points=q.max_points, status=status_for(points, q.max_points),
        method=GradingMethod.CODE_TESTS, feedback=f"{passed}/{total} test cases passed.\n" + "\n".join(lines),
        error_tags=tags,
    )
