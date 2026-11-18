# Rewrites

`wayward_eagle.c.reduce` rewrites a `c_ast.FuncDef`: `reduce_function(fd, noreturn, status)`
returns `[FuncDef]`, each piece `static`, the original last (`reduce_unit` and
`wayward-eagle reduce` do this for every function of a file; see
[the C layer](c.md#reducing)).

`cost(items, depth)` counts SonarSource's rules: an `if`, loop or `switch` one plus its depth,
an `else if`, an `else` and a `goto` one. `reduced` returns the functions (`Piece`: name,
parameters, statements, and what each returns: nothing, `STATUS` an exit code, or a
variable), the number of sections, and the status variables it added. It applies, in order:

| Rewrite | Applies to | Result |
|---|---|---|
| `sections` | a function returning nothing, at a top-level label no variable is used across and no goto crosses but into it | each section its own function (`NAME_K`), called in turn |
| `tails` | a function returning nothing, from its last top-level label back, each label nothing after it jumps back before | a function of the statements to the next label (`SECTION_LABEL`), taking the variables it reads before it sets them; each goto to it its call and a return |
| `flattened` | an `if` whose `else` ends in a jump and whose `then` does not, where that costs less | tested the other way, the jumping arm first |
| `ending` (`guarded`, loop bodies) | a function's or loop body's last `if`, or one whose arm leaves before the rest | one arm leaves first by `return` or `continue`, the other after it at the outer level, wherever that costs less once the moved statements end the level in turn |
| `extracted` | a function above `THRESHOLD` (25, SonarQube's default for C) | while above it, the nested block whose extraction saves the most, its call site counted, becomes a function (`PIECE_xK`) of the variables it reads: one leaving by one kind of exit (`return`, `break`, `continue`) returns whether it took it and its call is tested (`if (f(..)) return;`), one leaving by several returns a code its call stores in `PIECE_xK_k` and tests per exit, and one setting one variable used after it returns it (`v = f(.., v);`). A block a goto outside enters, that falls into the next case, that returns from a function returning a value, or that both leaves early and sets a variable used after it, stays. |

Every rewrite lowers the cost, so each terminates.

## Preparing and finishing

A whole function is prepared and finished thus:

- each local declaration becomes the assignment of its initializer, its type kept; each piece
  declares the variables it uses in the innermost statement list that sets them before each
  read, at the first assignment when that is a statement of the list, else at its start;
- a `switch` arm falling into the next ends in `__attribute__((fallthrough));`, so falling off
  an arm's end leaves the switch; when rendered, such an arm gets a `break;`;
- an `else` holding one `if` is an `else if`, an empty `else` none;
- a piece's assignments of a value computed from variables and constants alone to a local it
  never reads are dropped;
- a parameter a piece never reads is `(void)p;`;
- a function whose locals cannot move between functions (`static`, an array, an initializer
  list, its address taken, one name of two types), or holding a computed `goto`, is left as
  it is.

An extracted block's inputs are the variables it reads before setting them, a call to a
`noreturn` function ending a path. Its outputs are the variables it sets that statements able
to run after it read, when no loop around it and no goto out of that region can return
before it; otherwise those mentioned anywhere else in the function.
