"""Programmatic check of numerical answers.

The independent solver returns an arithmetic expression (only numbers,
+ - * / ** % //, parentheses and a few math functions). It is evaluated here
with a restricted AST walker, never with eval(), and the value is compared with
the numbers written in the expected answer.
"""
import ast
import math
import operator
import re

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {'sqrt': math.sqrt, 'abs': abs, 'round': round, 'min': min, 'max': max, 'log': math.log,
          'log10': math.log10, 'log2': math.log2, 'exp': math.exp, 'ceil': math.ceil, 'floor': math.floor,
          'sin': math.sin, 'cos': math.cos, 'tan': math.tan, 'factorial': math.factorial, 'comb': math.comb,
          'perm': math.perm}
_CONSTS = {'pi': math.pi, 'e': math.e}


class CalcError(ValueError):
    pass


def _walk(node, depth=0):
    if depth > 60:
        raise CalcError('Слишком сложное выражение.')
    if isinstance(node, ast.Expression):
        return _walk(node.body, depth + 1)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _walk(node.left, depth + 1), _walk(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise CalcError('Слишком большая степень.')
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_walk(node.operand, depth + 1))
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
            and not node.keywords and len(node.args) <= 4):
        args = [_walk(a, depth + 1) for a in node.args]
        if node.func.id == 'factorial' and (not args or args[0] > 170):
            raise CalcError('Слишком большой факториал.')
        return _FUNCS[node.func.id](*args)
    raise CalcError('Недопустимый элемент выражения.')


def evaluate_expression(expression):
    text = (expression or '').strip().replace('^', '**').replace('×', '*').replace('·', '*').replace('÷', '/').replace('−', '-')
    if not text or len(text) > 400:
        raise CalcError('Пустое или слишком длинное выражение.')
    try:
        tree = ast.parse(text, mode='eval')
        # Commas inside calls separate arguments: max(2,3) must stay max(2,3).
        # A top-level tuple such as 2,5*2 is instead decimal-comma notation.
        if isinstance(tree.body, ast.Tuple):
            text = re.sub(r'(?<=\d),(?=\d{1,2}\b)', '.', text)
            tree = ast.parse(text, mode='eval')
        value = _walk(tree)
    except (SyntaxError, ZeroDivisionError, OverflowError, TypeError, ValueError) as exc:
        raise CalcError(str(exc)) from None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CalcError('Результат не является конечным числом.')
    return float(value)


NUMBER = re.compile(r'(?<![\w.])[-−]?\d+(?:[  ]\d{3})*(?:[.,]\d+)?(?:\s*/\s*\d+)?')


def numbers_in(text):
    values = []
    for match in NUMBER.finditer(text or ''):
        raw = match.group(0).replace('−', '-').replace(' ', '').replace(' ', '')
        try:
            if '/' in raw:
                a, b = raw.split('/')
                values.append(float(a.replace(',', '.')) / float(b))
            else:
                values.append(float(raw.replace(',', '.')))
        except (ValueError, ZeroDivisionError):
            continue
    return values


def close(a, b):
    return math.isclose(a, b, rel_tol=0.011, abs_tol=1e-9)


def check(expression, expected_text):
    """Return (status, value): status is 'match', 'mismatch' or 'skipped'."""
    if not expression:
        return 'skipped', None
    try:
        value = evaluate_expression(expression)
    except CalcError:
        return 'skipped', None
    expected = numbers_in(expected_text)
    # Multiple numbers may be intermediate steps, bounds, or rubric weights.
    # They cannot establish which number is the final answer.
    if len(expected) != 1:
        return 'skipped', value
    number = expected[0]
    matches = close(value, number)
    if '%' in expected_text:
        matches = matches or close(value * 100, number)
    return ('match' if matches else 'mismatch'), value
