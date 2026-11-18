"""wayward-eagle: lower the cognitive complexity of C function bodies, keeping what they do."""

from .cost import cost
from .reduce import (
    THRESHOLD,
    Function,
    calling,
    ending,
    extracted,
    flattened,
    guarded,
    reduced,
    returning,
    sections,
    tails,
)
from .tree import (
    CONTINUE,
    RETURN,
    assigned_first,
    children,
    ends_in_jump,
    names_in,
    order,
    own,
    walk,
)

__all__ = [
    "CONTINUE",
    "Function",
    "RETURN",
    "THRESHOLD",
    "assigned_first",
    "calling",
    "children",
    "cost",
    "ending",
    "ends_in_jump",
    "extracted",
    "flattened",
    "guarded",
    "names_in",
    "order",
    "own",
    "reduced",
    "returning",
    "sections",
    "tails",
    "walk",
]
