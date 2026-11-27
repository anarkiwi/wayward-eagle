"""Constant arguments of the pieces a function is cut into: a parameter every call of a piece
passes one constant for (known from the path to the call) substituted and dropped; one passed
a few constants, the piece inlined when one statement, else specialised per value where
that strictly lowers the cognitive complexity and adds no memory access (docs/rewrites.md)."""

from __future__ import annotations

import copy

from pycparser import c_ast

from . import ints
from . import query as q
from .flow import parameters
from .stmt import block, braced, children, ends_in_jump, fresh, rebuild

#: The most constants one parameter may be passed for its piece to be specialised.
VALUES = 2
#: The most statements a piece inlined at each of its calls may hold.
INLINE = 1


def specialised(functions, taken=frozenset()):
    """``functions`` (pieces first, the function cut last: its signature stays) with each
    piece's constant parameters gone (:class:`_Pieces`), until none is left."""
    return _Pieces(functions, taken).run()


class _Pieces:  # pylint: disable=too-few-public-methods
    def __init__(self, functions, taken):
        self.funcs = list(functions)
        self.taken = set(taken) | {f.decl.name for f in functions}

    def run(self):
        """The functions, rewritten until no piece takes a constant parameter."""
        for _ in range(4 * len(self.funcs) + 4):
            if not self._step():
                break
        return self.funcs

    def _pieces(self):
        return {f.decl.name: f for f in self.funcs[:-1]}

    def _step(self):
        """Whether one piece parameter went."""
        pieces = self._pieces()
        sites = _sites(self.funcs, pieces)
        for name, f in pieces.items():
            calls = sites.get(name, [])
            if not calls or any(c is None for c in calls):
                continue
            for k, p in enumerate(parameters(f)):
                if p.name in q.writes(f.body) or p.name in q.addressed(f.body):
                    continue
                values = {c[k] for c in calls}
                if None in values:
                    continue
                if len(values) == 1:
                    self._substitute(f, k, values.pop())
                    return True
                if len(values) <= VALUES and self._split(f, k, sorted(values)):
                    return True
        return False

    def _substitute(self, f, k, value):
        """Piece ``f`` with parameter ``k`` the constant ``value``, dropped at every call."""
        name, param = f.decl.name, parameters(f)[k].name
        new = _without(f, k, value, param)
        self.funcs = [new if g is f else _dropped(g, name, {k: None}) for g in self.funcs]

    def _split(self, f, k, values):
        """Piece ``f`` inlined at its calls (where it fits), else specialised per value of
        parameter ``k`` where that strictly lowers the total cognitive complexity and adds
        no memory access; whether it was either."""
        name, param = f.decl.name, parameters(f)[k].name
        to_type = parameters(f)[k].type
        pinned = [_redirected(g, name, k, None, to_type) for g in self.funcs]
        best = _inlined(pinned, pinned[self.funcs.index(f)])
        if best is None:
            names = [fresh(f"{name}_{v}", self.taken) for v in values]
            copies = [_without(f, k, v, param, n) for v, n in zip(values, names)]
            spec = [h for g in self.funcs for h in ([g] if g is not f else copies)]
            spec = [_redirected(g, name, k, dict(zip(values, names)), to_type) for g in spec]
            if not _better(spec, self.funcs):
                return False
            best = spec
        self.funcs = best
        self.taken |= {g.decl.name for g in best}
        return True

    def _fresh(self, base):
        got = fresh(base, self.taken)
        self.taken.add(got)
        return got


def _better(new, old):
    """Whether functions ``new`` have a lower total cognitive complexity than ``old`` and
    access memory (subscripts and dereferences) no more often."""
    return _complexity(new) < _complexity(old) and _accesses(new) <= _accesses(old)


def _complexity(functions):
    return sum(q.cognitive(f.body) for f in functions)


def _accesses(functions):
    return sum(
        isinstance(n, c_ast.ArrayRef) or (isinstance(n, c_ast.UnaryOp) and n.op == "*")
        for f in functions
        for n in q.walk(f.body)
    )


# Values at call sites.


def _sites(functions, pieces):
    """``{piece: [values per argument, or None for a call not at statement level]}`` of every
    call of a piece in ``functions``: each argument's constant (in its parameter's type, the
    same in both data models) on the path to the call, else None."""
    out = {}
    for f in functions:
        known = _Known(f)
        for call, facts in known.calls():
            name = q.callee(call)
            if name not in pieces:
                continue
            params = parameters(pieces[name])
            args = q.arguments(call)
            if len(args) != len(params):
                out.setdefault(name, []).append(None)
                continue
            out.setdefault(name, []).append(
                tuple(known.value(a, facts, p.type) for a, p in zip(args, params))
            )
        for n in q.walk(f.body):
            if isinstance(n, c_ast.ID) and n.name in pieces and not known.called(n):
                out.setdefault(n.name, []).append(None)
    return out


class _Known:
    """The variables of function ``f`` stored once (a parameter never, a local by its
    initializer alone), whose values the conditions on a path fix there."""

    def __init__(self, f):
        self.f = f
        self.types = {p.name: p.type for p in parameters(f)}
        self.inits = {}
        writes = [c for c in q.walk(f.body) if isinstance(c, c_ast.Decl) and c.name]
        stored = _store_counts(f.body)
        for d in writes:
            self.types[d.name] = d.type
            if d.init is not None and stored.get(d.name, 0) == 1:
                self.inits[d.name] = d.init
        self.stable = {
            v
            for v in self.types
            if (v in self.inits or stored.get(v, 0) == 0) and v not in q.addressed(f.body)
        }
        self.stable -= {p.name for p in parameters(f) if p.name in q.writes(f.body)}
        self._callees = set()

    def called(self, n):
        """Whether ``n`` names the function a call found by :meth:`calls` calls."""
        return id(n) in self._callees

    def calls(self):
        """``(call, facts)`` of each call in the body, ``facts`` the ``(condition, truth)``
        holding on every path to it."""
        out = []
        self._walk(block(self.f.body), [], out)
        for call, _ in out:
            self._callees.add(id(call.name))
        return out

    def _walk(self, items, facts, out):
        facts = list(facts)
        for st in items:
            if isinstance(st, c_ast.If):
                self._exprs(st.cond, facts, out)
                then, els = block(st.iftrue), block(st.iffalse)
                self._walk(then, facts + [(st.cond, True)], out)
                self._walk(els, facts + [(st.cond, False)], out)
                if ends_in_jump(then) and not ends_in_jump(els):
                    facts.append((st.cond, False))
                elif ends_in_jump(els) and not ends_in_jump(then):
                    facts.append((st.cond, True))
            elif children(st):
                for part in ("cond", "init", "next"):
                    if getattr(st, part, None) is not None:
                        self._exprs(getattr(st, part), facts, out)
                for sub in children(st):
                    self._walk(sub, facts, out)
            else:
                self._exprs(st, facts, out)

    @staticmethod
    def _exprs(n, facts, out):
        out += [(c, facts) for c in q.walk(n) if isinstance(c, c_ast.FuncCall)]

    def value(self, e, facts, to_type):
        """The value of ``e`` given ``facts``, converted to ``to_type``, the same in both
        data models, else None."""
        got = set()
        for bits in ints.MODELS:
            env = self._env(facts, bits)
            x = ints.value(e, env, bits) if not _reads_other(e, env) else None
            t = ints.type_of(to_type, bits)
            if x is None or t is None:
                return None
            got.add(t.wrap(x[0]))
        return got.pop() if len(got) == 1 else None

    def _env(self, facts, bits):
        env = {}
        for cond, truth in facts:
            for var, x in _fixed(cond, truth):
                t = ints.type_of(self.types.get(var), bits) if var in self.stable else None
                if t is not None and t.holds(x):
                    env.setdefault(var, (x, t))
        for _ in range(len(self.inits) + 1):
            more = False
            for var, init in self.inits.items():
                if var in env or var not in self.stable:
                    continue
                t = ints.type_of(self.types[var], bits)
                x = ints.value(init, env, bits) if t is not None else None
                if x is not None:
                    env[var] = (t.wrap(x[0]), t)
                    more = True
            if not more:
                break
        return env


def _store_counts(body):
    out = {}
    for c in q.walk(body):
        if isinstance(c, c_ast.Decl) and c.name and c.init is not None:
            out[c.name] = out.get(c.name, 0) + 1
        var = q.variable(q.stored(c))
        if var is not None:
            out[var.name] = out.get(var.name, 0) + 1
    return out


def _reads_other(e, env):
    return any(isinstance(c, c_ast.ID) and c.name not in env for c in q.walk(e))


def _fixed(cond, truth):
    """``[(variable, value)]`` condition ``cond`` being ``truth`` fixes."""
    if isinstance(cond, c_ast.UnaryOp) and cond.op == "!":
        return _fixed(cond.expr, not truth)
    if isinstance(cond, c_ast.ID):
        return [] if truth else [(cond.name, 0)]
    if isinstance(cond, c_ast.BinaryOp):
        if cond.op in ("&&", "||") and truth == (cond.op == "&&"):
            return _fixed(cond.left, truth) + _fixed(cond.right, truth)
        if cond.op in ("==", "!=") and truth == (cond.op == "=="):
            for a, b in ((cond.left, cond.right), (cond.right, cond.left)):
                x, y = (ints.value(b, {}, bits) for bits in ints.MODELS)
                if isinstance(a, c_ast.ID) and x is not None and y is not None and x[0] == y[0]:
                    return [(a.name, x[0])]
    return []


# Rewrites.


def _without(f, k, value, param, name=None):
    """Piece ``f`` (renamed ``name``) with parameter ``k`` gone, each read of it ``value``."""
    f = copy.deepcopy(f)
    args = f.decl.type.args
    args.params = [p for j, p in enumerate(args.params) if j != k] or [_void()]
    if name is not None:
        f.decl.name = name
        f.decl.type.type.declname = name
    f.body = simplified(_replaced(f.body, param, value))
    return f


def _void():
    return c_ast.Typename(
        None, [], None, c_ast.TypeDecl(None, [], None, c_ast.IdentifierType(["void"]))
    )


def _replaced(n, name, value):
    """``n`` with each read of variable ``name`` the expression ``value`` (an int its
    literal), ``n`` itself too."""
    value = ints.constant(value) if isinstance(value, int) else value
    wrapper = c_ast.Compound([n])
    for c in q.walk(wrapper):
        for attr, kid in list(_kids(c)):
            if isinstance(kid, c_ast.ID) and kid.name == name:
                _set(c, attr, copy.deepcopy(value))
    return wrapper.block_items[0]


def _kids(n):
    for attr in n.__slots__:
        if attr in ("coord", "__weakref__"):
            continue
        v = getattr(n, attr, None)
        if isinstance(v, c_ast.Node):
            yield attr, v
        elif isinstance(v, list):
            for j, x in enumerate(v):
                if isinstance(x, c_ast.Node):
                    yield (attr, j), x


def _set(n, attr, value):
    if isinstance(attr, tuple):
        getattr(n, attr[0])[attr[1]] = value
    else:
        setattr(n, attr, value)


def _dropped(f, name, ks):
    """Function ``f`` with argument ``k`` of each call of ``name`` dropped (``ks`` keys)."""
    f = copy.deepcopy(f)
    for c in q.walk(f.body):
        if isinstance(c, c_ast.FuncCall) and q.callee(c) == name and c.args is not None:
            exprs = [a for j, a in enumerate(c.args.exprs) if j not in ks]
            c.args = c_ast.ExprList(exprs) if exprs else None
    return f


def _redirected(f, name, k, names, to_type):
    """Function ``f`` with each call of ``name`` calling ``names[value of argument k]``
    (``to_type`` the parameter's type), that argument dropped; ``names`` None, that
    argument its value."""
    f = copy.deepcopy(f)
    known = _Known(f)
    for call, facts in known.calls():
        if q.callee(call) != name:
            continue
        value = known.value(q.arguments(call)[k], facts, to_type)
        if names is None:
            call.args.exprs[k] = ints.constant(value)
            continue
        call.name = c_ast.ID(names[value])
        exprs = [a for j, a in enumerate(call.args.exprs) if j != k]
        call.args = c_ast.ExprList(exprs) if exprs else None
    return f


def _inlined(functions, f):
    """``functions`` with piece ``f`` (no return but a last, at most :data:`INLINE`
    statements, its locals named nowhere in its callers) put in place of each call, a
    statement followed by the end of its function or a ``return;``; None where it does not
    fit."""
    body = block(f.body)
    name = f.decl.name
    if len(body) > INLINE or any(isinstance(n, c_ast.Return) for n in q.walk(f.body)):
        return None
    mine = {n.name for n in q.walk(f.body) if isinstance(n, c_ast.Decl) and n.name}
    params = [p.name for p in parameters(f)]
    out = []
    for g in functions:
        if g is f:
            continue
        if name not in {q.callee(c) for c in q.walk(g.body) if isinstance(c, c_ast.FuncCall)}:
            out.append(g)
            continue
        if mine & {n.name for n in q.walk(g.body) if isinstance(n, c_ast.ID)} | (
            mine & {n.name for n in q.walk(g.body) if isinstance(n, c_ast.Decl)}
        ):
            return None
        new = _put(block(g.body), name, body, params)
        if new is None:
            return None
        g = copy.deepcopy(g)
        g.body = braced(new)
        out.append(g)
    return out


def _put(items, name, body, params):
    """``items`` with each statement calling ``name`` replaced by ``body``, its parameters
    the call's arguments (variables or constants), or None where a call is elsewhere."""
    out = []
    for st in items:
        if isinstance(st, c_ast.FuncCall) and q.callee(st) == name:
            args = q.arguments(st)
            if not all(isinstance(a, (c_ast.ID, c_ast.Constant)) for a in args):
                return None
            got = copy.deepcopy(body)
            for p, a in zip(params, args):
                got = [_replaced(s, p, a) for s in got]
            out += [simplified(s) for s in got]
            continue
        if any(name == q.callee(c) for c in q.walk(st) if isinstance(c, c_ast.FuncCall)):
            if not children(st):
                return None
            subs = [_put(sub, name, body, params) for sub in children(st)]
            if any(s is None for s in subs):
                return None
            if isinstance(st, c_ast.If) and name in {
                q.callee(c) for c in q.walk(st.cond) if isinstance(c, c_ast.FuncCall)
            }:
                return None
            st = rebuild(st, subs)
            if isinstance(st, c_ast.If) and not block(st.iffalse):
                st.iffalse = None
        out.append(st)
    return out


# Simplification.

#: Operators whose identity element is 0 on the right.
RIGHT_ZERO = {"+", "-", "^", "|", "<<", ">>"}
#: Operators whose identity element is 0 on the left.
LEFT_ZERO = {"+", "^", "|"}


def simplified(n):
    """``n`` with each constant subexpression of type ``int`` (in both data models) its
    value, ``x op 0`` (``0 op x``) ``x`` where its parent promotes ``x`` anyway, ``K && x``
    and ``K || x`` decided by ``K`` (``x`` where only its truth is tested), and each ``if``
    of a constant condition the arm it takes."""
    _simplify(n, None)
    return n


def _simplify(n, _attr):
    for attr, kid in list(_kids(n)):
        _simplify(kid, attr)
        got = _folded(kid, n, attr)
        if got is not kid:
            _set(n, attr, got)
    if isinstance(n, c_ast.Compound) and n.block_items:
        n.block_items = [x for st in n.block_items for x in _taken(st)]


def _taken(st):
    """``[st]``, or the statements of the arm an ``if`` of a constant condition takes."""
    truth = _truth(st.cond) if isinstance(st, c_ast.If) else None
    if truth is None:
        return [st]
    return block(st.iftrue if truth else st.iffalse)


def _truth(e):
    """Whether constant ``e`` is nonzero in both data models, None when it is no constant."""
    vals = [ints.value(e, {}, b) for b in ints.MODELS]
    if any(v is None for v in vals) or any(isinstance(c, c_ast.ID) for c in q.walk(e)):
        return None
    truths = {v[0] != 0 for v in vals}
    return truths.pop() if len(truths) == 1 else None


def _tested(parent, attr):
    """Whether the expression at ``attr`` of ``parent`` has only its truth tested."""
    if isinstance(parent, (c_ast.If, c_ast.TernaryOp, *q.LOOPS)):
        return attr == "cond"
    if isinstance(parent, c_ast.UnaryOp):
        return parent.op == "!"
    return isinstance(parent, c_ast.BinaryOp) and parent.op in ("&&", "||")


def _folded(e, parent, attr=None):
    if _foldable(e) and not any(isinstance(c, c_ast.ID) for c in q.walk(e)):
        vals = [ints.value(e, {}, b) for b in ints.MODELS]
        ok = all(v is not None and v[1] == ints.Int(b, True) for v, b in zip(vals, ints.MODELS))
        if ok and vals[0][0] == vals[1][0] and vals[0][0] >= 0:
            return ints.constant(vals[0][0])
    if isinstance(e, c_ast.BinaryOp) and e.op in ("&&", "||"):
        truth = _truth(e.left)
        if truth is not None and truth == (e.op == "||"):
            return ints.constant(int(truth))
        if truth is not None and _tested(parent, attr):
            return e.right
    if isinstance(e, c_ast.BinaryOp):
        if e.op in RIGHT_ZERO and _zero(e.right) and _promotes(parent, e.left):
            return e.left
        if e.op in LEFT_ZERO and _zero(e.left) and _promotes(parent, e.right):
            return e.right
    return e


def _foldable(e):
    """Whether ``e`` is an operation a constant may stand for."""
    return isinstance(e, c_ast.BinaryOp) or (
        isinstance(e, c_ast.UnaryOp) and e.op in ("-", "~", "!")
    )


def _zero(e):
    return isinstance(e, c_ast.Constant) and ints.value(e, {}, 16) == (0, ints.Int(16, True))


def _promotes(parent, x):
    """Whether ``x op 0`` and ``x`` agree where ``parent`` holds it: ``parent`` applies the
    integer promotions (or the usual arithmetic conversions) to its operands, or ``x`` is
    an arithmetic result, promoted already."""
    if isinstance(x, c_ast.BinaryOp) and x.op not in ("&&", "||", ","):
        return True
    if isinstance(parent, c_ast.BinaryOp):
        return parent.op not in ("&&", "||", ",")
    return isinstance(parent, c_ast.UnaryOp) and parent.op in ("-", "~", "+")
