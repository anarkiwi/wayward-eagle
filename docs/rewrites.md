# Rewrites

`cost(items, depth)` counts SonarSource's rules: an `if`, loop or `switch` one plus its depth,
an `else if`, an `else` and a `goto` one. `reduced` returns the functions (`Piece`: name, parameters, statements, and what each returns: nothing, `STATUS` an exit code, or a variable), the number of sections, and the status variables it added. It applies, in order:

| Rewrite | Applies to | Result |
|---|---|---|
| `sections` | a function returning nothing, at a top-level label no variable is used across and no goto crosses but into it | each section its own function (`NAME_K`), called in turn |
| `tails` | a function returning nothing, from its last top-level label back, each label nothing after it jumps back before | a function of the statements to the next label (`SECTION_LABEL`), taking the variables it reads before it sets them; each goto to it its call and a return |
| `flattened` | an `if` whose `else` ends in a jump and whose `then` does not, where that costs less | tested the other way, the jumping arm first |
| `ending` (`guarded`, loop bodies) | a function's or loop body's last `if`, or one whose arm leaves before the rest | one arm leaves first by `return` or `continue`, the other after it at the outer level, wherever that costs less once the moved statements end the level in turn |
| `extracted` | a function above `THRESHOLD` (25, SonarQube's default for C) | while above it, the nested block whose extraction saves the most, its call site counted, becomes a function (`PIECE_xK`) of the variables it reads: one leaving by one kind of exit (`return`, `break`, `continue`) returns whether it took it and its call is tested (`if (f(..)) return;`), one leaving by several returns a code its call stores in `PIECE_xK_k` and tests per exit, and one setting one variable used after it returns it (`v = f(.., v);`). A block a goto outside enters, that falls into the next case, that returns from a function returning a value, or that both leaves early and sets a variable used after it, stays. |

Every rewrite lowers the cost, so each terminates.
