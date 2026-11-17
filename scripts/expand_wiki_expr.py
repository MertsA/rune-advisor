"""Evaluate wiki arithmetic templates so extracted rates can quote literal numbers.

Usage: python scripts/expand_wiki_expr.py <in_dir> <out_dir>

Guide tables often hold rates as templates that the wiki computes at render time, e.g.
{{#expr:(185*485/1000) round 0}},000 or {{formatnum:{{#expr: 58 * {{#var:furniture}}}}}}, so the
page source has no literal number to quote. Walking each page in order (innermost templates first),
this resolves:
  - {{#expr:...}} whose body is pure arithmetic: numbers, + - * / ^ ( ), ceil(), floor(), and a
    trailing MediaWiki `round N` (spaces optional)
  - {{#vardefine:name|value}} (records the value, renders nothing) and {{#var:name}}
  - {{formatnum:N}} around a number
Anything depending on other templates (GE prices, item values) is left untouched, so gp figures
stay unevaluated while xp figures become literal.
"""

from __future__ import annotations

import ast
import math
import operator
import re
import sys
from pathlib import Path

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"ceil": math.ceil, "floor": math.floor}
_PURE = re.compile(r"^[\d\s.,+\-*/^()]*$")
_ROUND = re.compile(r"^(?P<expr>.+?)\s*round\s*(?P<digits>-?\d+)\s*$")


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
            and len(node.args) == 1 and not node.keywords):
        return _FUNCS[node.func.id](_eval(node.args[0]))
    raise ValueError("unsupported expression")


def _format(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def evaluate(body: str) -> str | None:
    """Value of a pure-arithmetic #expr body, formatted like the wiki would, or None to leave it."""
    body = body.strip()
    digits = None
    if m := _ROUND.match(body):
        body, digits = m["expr"], int(m["digits"])
    if not _PURE.match(re.sub(r"\b(ceil|floor)\b", "", body)) or not body:
        return None
    try:
        value = _eval(ast.parse(body.replace(",", "").replace("^", "**"), mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        return None
    if digits is not None:
        value = round(value, digits)
    return _format(value)


class _Expander:
    def __init__(self) -> None:
        self.vars: dict[str, str] = {}
        self.replaced = 0
        self.left = 0

    def expand(self, text: str) -> str:
        """Expand templates in `text`, left to right, resolving inner templates first."""
        out, i = [], 0
        while (start := text.find("{{", i)) != -1:
            end = self._match(text, start)
            if end is None:
                break
            out.append(text[i:start])
            out.append(self._template(self.expand(text[start + 2:end - 2]), text[start:end]))
            i = end
        out.append(text[i:])
        return "".join(out)

    @staticmethod
    def _match(text: str, start: int) -> int | None:
        depth, j = 0, start
        while j < len(text):
            if text.startswith("{{", j):
                depth, j = depth + 1, j + 2
            elif text.startswith("}}", j):
                depth, j = depth - 1, j + 2
                if depth == 0:
                    return j
            else:
                j += 1
        return None

    def _template(self, inner: str, original: str) -> str:
        """Render one template whose arguments are already expanded."""
        name, _, arg = inner.partition(":")
        name = name.strip().lower()
        if name == "#expr":
            value = evaluate(arg)
            if value is None:
                self.left += 1
                return "{{" + inner + "}}"
            self.replaced += 1
            return value
        if name == "#vardefine":
            var, _, value = arg.partition("|")
            if evaluate(value) is not None:
                self.vars[var.strip()] = evaluate(value)
            return "" if evaluate(value) is not None else original
        if name == "#var":
            var = arg.split("|")[0].strip()
            return self.vars.get(var, "{{" + inner + "}}")
        if name == "formatnum":
            value = evaluate(arg)
            if value is not None:
                number = float(value)
                return f"{number:,.0f}" if number == int(number) else f"{number:,}"
        return "{{" + inner + "}}"


def expand(text: str) -> tuple[str, int, int]:
    """Expand evaluable templates. Returns (text, #expr evaluated, #expr left as templates)."""
    e = _Expander()
    result = e.expand(text)
    return result, e.replaced, e.left


def main(in_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(in_dir.glob("*.wiki")):
        text, replaced, left = expand(f.read_text())
        (out_dir / f.name).write_text(text)
        print(f"{f.name}: {replaced} evaluated, {left} left as templates", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
