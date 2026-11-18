# Statement trees

A function body is a list of statements, each a tuple:

| Statement | Form |
|---|---|
| assignment | `("a", name, text, refs)` |
| statement | `("s", text, refs)` |
| jump | `("j", text, refs)`: `return;`, `break;`, `continue;`, `goto L;`, `__attribute__((fallthrough));` |
| label | `("l", "L:;", refs)` |
| if | `("if", cond, refs, then, else, negated cond)` |
| loop | `("loop", kind, cond, refs, body)`, kind `for`, `while` or `do` |
| switch | `("switch", selector, refs, [(labels, body)])` |

`refs` are the variables a statement reads outside its nested statements. Texts are opaque to
the rewrites except for the jump texts above; a printer may carry markers in them.

`Function(params, local, void, noreturn)` tells the rewrites the function's parameters, its
other variables, whether it returns nothing, and which statements are calls that never return.
