"""Integer data models: each standard integer type's range and width as clang evaluates
them for a target, and C's integer promotions over them."""

from __future__ import annotations

import functools
import pathlib
import tempfile
from typing import NamedTuple

from .front import load


class Int(NamedTuple):
    """An integer type's range, width, and whether conversion to it wraps modulo its width."""

    lo: int
    hi: int
    bits: int
    wraps: bool

    def holds(self, other):
        """Whether every value of ``other`` is a value of this type."""
        return self.lo <= other.lo and other.hi <= self.hi


#: The standard integer types by clang's canonical spelling, ``_Bool`` apart.
INTEGERS = (
    "char",
    "signed char",
    "unsigned char",
    "short",
    "unsigned short",
    "int",
    "unsigned int",
    "long",
    "unsigned long",
    "long long",
    "unsigned long long",
    "__int128",
    "unsigned __int128",
)
#: A translation unit whose enumerators are each type's width, and whether ``char`` is
#: signed, as clang evaluates them for a target.
PROBE = "enum {\n%s\n  char_signed = (char)-1 < 0\n};\n" % "\n".join(
    ("#ifdef __SIZEOF_INT128__\n" if "__int128" in t else "")
    + f"  w{k} = sizeof({t}) * __CHAR_BIT__,"
    + ("\n#endif" if "__int128" in t else "")
    for k, t in enumerate(INTEGERS)
)


@functools.cache
def model(target):
    """``{canonical integer type: Int}`` of ``target``'s data model: the widths clang gives
    the enumerators of :data:`PROBE`; an enum is ``int``."""
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "probe.c"
        path.write_text(PROBE, encoding="utf-8")
        unit = load(path, target=target)
    (decl,) = unit.ast.ext
    values = {e.name: int(unit.info(e.value).value) for e in decl.type.values.enumerators}
    out = {"_Bool": Int(0, 1, 1, False)}
    for k, name in enumerate(INTEGERS):
        if f"w{k}" in values:
            signed = not name.startswith("unsigned") and (name != "char" or values["char_signed"])
            out[name] = sized(values[f"w{k}"], signed)
    return out


def sized(bits, signed):
    """The ``Int`` of a ``bits``-wide integer type, ``signed`` or not."""
    if signed:
        return Int(-(1 << (bits - 1)), (1 << (bits - 1)) - 1, bits, True)
    return Int(0, (1 << bits) - 1, bits, True)


def width(types, typ):
    """The ``Int`` of :class:`~wayward_eagle.c.Type` ``typ`` in ``types``, None for a type no
    integer."""
    if typ is None:
        return None
    canon = typ.canon
    for qual in ("const ", "volatile "):
        canon = canon.replace(qual, "")
    if canon.startswith("enum "):
        return types["int"]
    return types.get(canon)


def promoted(types, t):
    """``Int`` ``t`` after the integer promotions in ``types``."""
    native = types["int"]
    if t.bits < native.bits or not t.wraps:
        return native if native.holds(t) else types["unsigned int"]
    return t
