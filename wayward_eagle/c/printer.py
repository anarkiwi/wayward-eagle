"""C text of ``c_ast`` nodes: pycparser's generator, GNU attributes, macros by name, ``#line``
markers, then the pinned clang-format."""

from __future__ import annotations

import subprocess

import clang_format
from pycparser import c_ast, c_generator

from .front import Opaque

#: Binary operators by group, for :data:`BARE`.
GROUPS = {
    **dict.fromkeys(("*", "/", "%"), "mul"),
    **dict.fromkeys(("+", "-"), "add"),
    **dict.fromkeys(("<<", ">>"), "shift"),
    **dict.fromkeys(("<", ">", "<=", ">=", "==", "!="), "cmp"),
    **dict.fromkeys(("&", "^", "|"), "bit"),
    "&&": "and",
    "||": "or",
}
#: ``(operand group, operator group)`` pairs printed without parentheses when the operand
#: binds tighter: those ``-Wparentheses`` and readers take as read.
BARE = frozenset(
    {("mul", "add")}
    | {(a, b) for a in ("mul", "add", "shift") for b in ("cmp", "and", "or")}
    | {("cmp", "and"), ("cmp", "or")}
)
#: Operators whose left operand of the same operator needs no parentheses.
ASSOCIATIVE = frozenset(("+", "*", "&", "|", "^", "&&", "||", "-", "/", "%", "<<", ">>"))


def _group(op):
    return GROUPS.get(op, op)


#: The clang-format style rendered C is laid out in.
STYLE = "{BasedOnStyle: LLVM, MaxEmptyLinesToKeep: 0, RemoveParentheses: MultipleParentheses}"


class Generator(c_generator.CGenerator):
    """pycparser's C generator printing attributes as GNU ``__attribute__((...))``, each node
    of ``macros`` (``{node: name}``, as :attr:`Unit.macros` holds) as its macro's name, a
    ``#line`` marker before each statement of ``lines`` (``{node: (line, file)}``), a
    computed ``goto`` and each :class:`~wayward_eagle.c.front.Opaque` node as its text."""

    def __init__(self, macros=None, lines=None):
        super().__init__(reduce_parentheses=True)
        self.macros, self.lines = macros or {}, lines or {}

    def visit(self, node):
        name = self.macros.get(node)
        return name if name is not None else super().visit(node)

    def _is_simple_node(self, n):
        return n in self.macros or super()._is_simple_node(n)

    def _operand(self, d, op, right):
        """Operand ``d`` of binary operator ``op``, parenthesized unless it binds tighter in
        a pairing no compiler warns of (:data:`BARE`)."""
        if self._is_simple_node(d) or isinstance(d, (c_ast.UnaryOp, c_ast.Cast)):
            return self.visit(d)
        if isinstance(d, c_ast.BinaryOp):
            p, q = self.precedence_map[d.op], self.precedence_map[op]
            if d.op == op and op in ASSOCIATIVE and not right:
                return self.visit(d)
            if p > q and (_group(d.op), _group(op)) in BARE:
                return self.visit(d)
        return "(" + self.visit(d) + ")"

    def visit_BinaryOp(self, n):  # pylint: disable=invalid-name
        """``a op b``, casts and unary operators as tightly bound as they are."""
        return f"{self._operand(n.left, n.op, False)} {n.op} {self._operand(n.right, n.op, True)}"

    def visit_Cast(self, n):  # pylint: disable=invalid-name
        """``(T)e``, a cast or unary operand unparenthesized."""
        inner = n.expr
        if isinstance(inner, (c_ast.Cast, c_ast.UnaryOp)) or self._is_simple_node(inner):
            operand = self.visit(inner)
        else:
            operand = "(" + self.visit(inner) + ")"
        return "(" + self._generate_type(n.to_type, emit_declname=False) + ")" + operand

    def visit_TernaryOp(self, n):  # pylint: disable=invalid-name
        """``c ? a : b``, each operand parenthesized unless simple."""
        return " ".join(
            (
                self._parenthesize_unless_simple(n.cond),
                "?",
                self._parenthesize_unless_simple(n.iftrue),
                ":",
                self._parenthesize_unless_simple(n.iffalse),
            )
        )

    def visit_AttributeSpecifier(self, n):  # pylint: disable=invalid-name
        """``__attribute__((a, b))``"""
        return "__attribute__((" + ", ".join(self.visit(a) for a in n.attributes) + "))"

    def visit_Goto(self, n):  # pylint: disable=invalid-name
        """``goto L;`` or ``goto *e;``"""
        if isinstance(n.name, c_ast.Node):
            return "goto *" + self.visit(n.name) + ";"
        return super().visit_Goto(n)

    def visit_Opaque(self, n):  # pylint: disable=invalid-name
        """A construct ``c_ast`` has no node for, as its source text."""
        return n.text or ""

    def _generate_stmt(self, n, add_indent=False):
        if isinstance(n, Opaque) and n.kind.endswith("Stmt"):
            n = c_ast.ExprList([n])
        out = super()._generate_stmt(n, add_indent)
        at = self.lines.get(n)
        return out if at is None else f'#line {at[0]} "{at[1]}"\n' + out


def text(node, macros=None, lines=None):
    """The C text of ``node`` (:class:`Generator`), unformatted."""
    return Generator(macros, lines).visit(node)


def format_c(source, style=STYLE):
    """C text ``source`` laid out by the pinned clang-format."""
    run = subprocess.run(
        [clang_format.get_executable("clang-format"), f"--style={style}"],
        input=source,
        capture_output=True,
        text=True,
        check=True,
    )
    return run.stdout


def render(nodes, macros=None, lines=None, style=STYLE):
    """The formatted C text of the ``c_ast`` declarations ``nodes``."""
    gen = Generator(macros, lines)
    out = [gen.visit(n) + ("" if isinstance(n, c_ast.FuncDef) else ";") for n in nodes]
    return format_c("\n".join(out), style)
