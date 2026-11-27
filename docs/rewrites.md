# Rewrites

`wayward_eagle.c.reduce` rewrites a `c_ast.FuncDef`:
`reduce_function(fd, noreturn, status, taken, outer)` returns `[FuncDef]`, each piece `static` and
named apart from `taken` and every name `fd` mentions, the original last (`reduce_unit` and
`wayward-eagle reduce` do this for every function of a file, taking the file's names; see
[the C layer](c.md#reducing)).

`cost(items, depth)` counts SonarSource's rules: an `if`, loop or `switch` one plus its depth,
an `else if`, an `else` and a `goto` one. A function is

1. prepared ([below](#preparing-and-finishing)) and refined (`refine.refined`, then
   `refine.Rotation` with widening, [below](#refinement));
2. cut by `reduced` into functions (`Piece`: name, parameters, statements, and what each
   returns: nothing, `STATUS` an exit code, or a variable), with the section names and the
   status variables it added, applying in order:

| Rewrite | Applies to | Result |
|---|---|---|
| `sections` | a function returning nothing, at a top-level label no variable is used across, no goto crosses but into it, and no `return` before it skips (one after a call that never returns does not) | each section its own function (`NAME_K`), called in turn |
| `tails` | a function returning nothing, from its last top-level label back, each label nothing after it jumps back before | a function of the statements to the next label (`SECTION_LABEL`), taking the variables live into it; each goto to it its call and a return |
| `flattened` | an `if` whose `else` ends in a jump and whose `then` does not, where that costs less | tested the other way, the jumping arm first |
| `ending` (`guarded`, loop bodies) | a function's or loop body's last `if`, or one whose arm leaves before the rest | one arm leaves first by `return` or `continue`, the other after it at the outer level, wherever that costs less once the moved statements end the level in turn |
| `extracted` | a function above `THRESHOLD` (25, SonarQube's default for C) | while above it, the nested block whose extraction saves the most, its call site counted, becomes a function (`PIECE_xK`) taking the variables live into it that the rest of the function mentions: one leaving by one kind of exit (`return`, `break`, `continue`) returns whether it took it and its call is tested (`if (f(..)) return;`), one leaving by several returns a code its call stores in `PIECE_xK_k` and tests per exit, and one setting one variable live wherever control goes on leaving it returns it (`v = f(..);`, taking `v` too when a path through it leaves `v` unset). A block a goto outside enters, that falls into the next case, that returns from a function returning a value, or that both leaves early and sets a variable live after it, stays. |

3. finished: each piece refined again (without widening), then copies propagated,
   single-use temporaries folded, dead stores and unreached statements dropped, trailing jumps trimmed, and parameters pruned
   ([below](#finishing)).

Every rewrite of step 2 lowers the cost, so each terminates.

## Refinement

`refine.refined` rewrites each statement list, nested lists first:

| Rewrite | Applies to | Result |
|---|---|---|
| `common` | the same statement (structurally) leading or ending every arm of an `if`/`else if`/`else` chain ending in `else`, or of a `switch` with a `default` (a `switch` arm's trailing `break` aside), the arms not all alike; never a label, an empty statement or a fall into the next case | leading: moved before it when the conditions store nothing, hold no comma and call nothing, the statement stores no variable they read, and, when they read more than the function's own variables (parameters and locals: a global, an array, a member, a dereference, a call), it stores only those and calls nothing; in a `switch`, not a `break` nor one holding a `break` of it. Ending: moved after it; in a `switch`, only when no arm leaves it by an inner `break` or falls into the next. A lone `if` left with an empty first arm tests the other way. |
| `sequenced` | an infinite loop (`for (;;)`, `while (1)`) whose body is `A; if (c) { S; continue; } T`, with `T` always leaving the loop by `break` or `return`, no `continue` of it in `T`, no `break` of it in `A` or `S`, and no label or goto in the loop, where that costs less | `for (;;) { A; if (!c) break; S }` then `T`, each `break` of the loop ending `T` (or an arm of an `if` ending it) dropped; an `if` before `T`'s end with one arm ending in `break` and the other going on to statements that leave by `return`, tested for that other arm first, holding the rest |
| `shaped` | a loop whose condition stores or holds a comma | `for (;;)` evaluating it in statements: a comma's operands in turn, `a && b` as `if (!a) break;` then `b`; for a `do` loop, at the end and only with no `continue` |
| | an infinite loop whose body starts with `if (c) break;` | `while (!c)` (a `for` with an init or step keeps them and tests `!c`) |
| | an infinite loop, no init or step, whose body ends with `if (c) break;` and holds no `continue` of it | `do ... while (!c);` |
| `counted` | `while (c)` with a variable `v` that `c` reads and does not store (dewolf's rules, relaxed from DREAM++'s): the last statement before the loop mentioning `v` is `v = e;`, `e` not reading `v`; the statements between hold no label and no jump out, and, when there are any, `e` reads only the function's own variables, none of which they store; one statement storing `v` alone (`v = ..`, `v op= ..`, `++v`) ends every path of the body that continues (past its end, through the arms of an `if` ending it, or before each `continue`); the body stores `v` nowhere else; no label or goto in the loop | `for (v = e; c; step)`, the step dropped from the body |

`refine.Rotation` then makes each loop testing its exit before the steps ending its body a
`for` testing at the head:

- shapes: `for (;;) { B; if (x) break; S }` with no `continue` of the loop in `B`, or
  `do B while (c && (S, k))` with `k` a nonzero literal (`x` is `!c`; `B` may `continue`);
  `S` one or more statements each storing one variable a value of variables and constants;
- `x` reads exactly one variable `v` and constants; one statement of `S` stores `v`, from `v`
  and constants; `B` does not store `v`; the last statement before the loop mentioning `v`
  sets it as `counted` requires, to a constant; the other variables `S` stores are dead
  after the loop; no label or goto in the loop;
- `v` live after the loop: allowed only when no `break` of the loop is in `B`, and `v = last;`
  follows the loop.

The head condition is derived exactly. With `v`'s declared type (8 or 16 bits) and C's
integer rules under a 16-bit `int` and again under a 32-bit `int` (`wayward_eagle.c.ints`:
literal types, integer promotions, usual arithmetic conversions, wrapping on conversion,
signed overflow, division by zero and bad shifts undefined), `v` runs from its init: at each
value the body runs, `x` is evaluated, and if false the step gives the next value. This
yields the values `v` holds on each run of the body, `v0 .. vm` (`x` first true at `vm`), and
the value `e` one more step would give. The first run is bounded by the number of values of
the type, the second by the first's length; the sequences must agree, or the loop stays.

A head condition `H` is exact when `H` holds at every `vi` and fails at `e`, in both models.
The candidates are comparisons of a side `f`, either `v` itself or an operand reading `v` of
a comparison in `x`, with a literal, over the values `f(v0) .. f(vm), f(e)`:

- when they strictly rise: `f < f(e)` and `f <= f(vm)`; when they strictly fall: `f > f(e)` and
  `f >= f(vm)` (an ordered comparison only when the sequence is monotone without wrapping);
- `f != f(e)` when `f(e)` is none of the others.

Each candidate is checked by evaluation, and the one kept prefers, in order: a literal that
appears in `x`, an ordered comparison over `!=`, `v` over another side, a strict comparison.
So `v = 2; for (;;) { ..; if (v == 0) break; v = v - 1; }` over a `uint8_t` gives
`v != 0xFF`, and `do { .. } while ((int8_t)v > 0 && (v = v - 1, 1))` from 2 gives
`(int8_t)v >= 0`.

When no candidate exists (the values wrap to one already taken: `v = 0`, step 2, exit at
`0xFE`) and, before extraction, nothing but the loop and its init mentions `v`, it is dead
after the loop, never under `sizeof`, and its type promotes to `int` in both models, `v` is
declared `int`: each read gives the value it gave (the promoted one), and the sequence,
recomputed in `int`, must hold the same values; the head then tests the unwrapped values
(`v <= 0xFE`).

## Preparing and finishing

A whole function is prepared and finished thus:

- each local declaration becomes the assignment of its initializer, its type kept; each piece
  declares the variables it uses in the innermost statement list that sets them before each
  read, at the first assignment when that is a statement of the list, in a `for`'s init when
  that loop alone mentions it, else at the list's start;
- a `switch` arm falling into the next ends in `__attribute__((fallthrough));`, so falling off
  an arm's end leaves the switch; when rendered, such an arm gets a `break;`;
- an `else` holding one `if` is an `else if`, an empty `else` none;
- a function whose locals cannot move between functions (`static`, an array, an initializer
  list, its address taken, one name of two types), or holding a computed `goto`, is left as
  it is.

### Finishing

Over each piece's control flow graph (`wayward_eagle.c.listflow`: a node per expression
evaluated, a call to a `noreturn` function ending its path; liveness, available copies,
reachability):

- a copy `v = u`, `u` a variable of `v`'s type or an integer literal whose value `v`'s type
  holds and which promotes to `int` in both models, is read in place of `v` wherever it is
  available (on every path the last store to `v` is that copy and `u` is not stored since),
  when it is available at every read of `v` it reaches and no such read is in an expression
  storing `u`, or storing `v` other than as `v = ..` not storing it again;
- *single-use temporaries folded* (`wayward_eagle.c.fold`, dewolf's expression propagation):
  a store `v = e;` (a statement) to a local is dropped and `e` read in place of `v` at the
  one read of `v` it reaches, when
  - `e` stores nothing, holds no comma, does not read `v` and touches nothing `volatile`;
  - that read is reached by no other store to `v` and not from the entry without it, and
    is not in a loop the store is not (it runs once each time the store does);
  - nothing on a path between them stores what `e` reads (a local `e` reads, or `v`), nor,
    when `e` reads memory (anything but a local; naming a file-scope array reads nothing),
    calls or stores to memory; when `e` calls or stores to memory, the read is the very
    next evaluation and is not under `&&`, `||`, `?:` or a comma;
  - the rest of the use's expression, but what acts after the read (the calls and stores
    enclosing it), does not clash with `e` the same way, and the read is not under `sizeof`;
  - the value is the same: the read's context converts it to `v`'s type (a store `x = v`
    to an object of that type, an argument to a parameter of that type, a `return` from a
    function returning it), or `v`'s type holds every value `e` may take (its range from
    C's conversions over the types of locals and of `outer`'s objects, arrays and
    functions' returns) and either the context needs only the value (a converted one as
    before, a condition, an operand of `!`, `&&`, `||`) or `e`'s type promotes as `v`'s
    does, in both data models;
  - the use's expression then holds at most `fold.SIZE` (24) nodes, and the function's
    cognitive complexity does not rise;

  repeated until none applies (one fold may open another, folding a chain into its end);
- *carry chains as sums* (`wayward_eagle.c.carry`, only with `carries`), in each piece after
  it is finished, then temporaries folded again; each over locals typed `uint8_t` that one
  piece alone mentions and none takes, and kept only where it lowers the piece's cognitive
  complexity, else its statements, else its nodes:
  - a byte sum `x = a + b;` (the one store to `x`, no read before it; `a`, `b` bytes in both
    data models, by the ranges folding uses) whose carry is recomputed, `x < a` or `x < b`
    (`a > x`, ..; the operand a local unchanged since, or a constant), is widened:
    `uint16_t x = a + b;` holds the exact sum, the carry reads `x > 0xFF` (an `int` 0 or 1,
    as the comparison was), and each other read `(uint8_t)x`, uncast where its context
    converts to a byte or casts to 8 bits or fewer;
  - a widened `x = (uint8_t)t + c` (`c` 0 or 1) of a widened `t`, every carry of `x` in
    `t > 0xFF || x > 0xFF` and `t` read nowhere else, becomes `x = t + c` with each such
    `||` `x > 0xFF`, then `t`'s sum folded in where folding's order rules allow
    (`x = a + b + c`); a chain left with a carry in an `||` stays bytes;
  - `(uint16_t)(((uint16_t)H << 8 | L) + ((uint16_t)T << 8 | C)) >> 8` of bytes, with no
    effect, where its context converts it or needs only its value, becomes
    `(uint8_t)(H + T + (L + C > 0xFF))`, uncast where that context converts to a byte;
- a store to a local dead after it goes when its value is computed from variables and
  constants alone, and becomes its value as a statement when that calls or stores;
- statements control never reaches go;
- a `return;` ending a function returning nothing, or ending each arm of an `if` ending it,
  goes, as does a `continue;` ending a loop body or the arms of an `if` ending it; an `if`
  left with no arms and a condition of variables and constants goes;
- *joins called* (`joined`): a status piece returning only "go on" (0) or "return" (1),
  tested by one site its caller (returning nothing) reaches through `if` arms, runs what
  follows the site to its caller's end in place of each `return 0` and returns nothing; its
  site is the call and a return, and the join's live variables become its parameters, until
  no piece is left so; a join holding code (a statement other than a jump, a return of a
  variable or constant, or a call of variables and constants) that would then be in two
  places (two `return 0`, or code after the site's own list its caller keeps for other
  paths) leaves the piece a status;
- *sunk tails* (`wayward_eagle.c.sink`): a label every `goto` to which lies in the statements
  before it in its own list goes, the statements between the first of them and the label
  put on each path falling to it (an `if` not taken, an arm's end, before a `break` of a
  `switch` the `goto` breaks out of), each `goto` leaving, where that keeps or lowers the
  cost; a `goto` inside a loop, a label also reached from after it, or a tail holding code
  that would go on two paths (no statement of code is copied), stays;
- *joins by condition* (`wayward_eagle.c.joins`), nested lists first:
  - an infinite loop left by one `break` of its own and by gotos to one label outside it
    (none in a loop or `switch` nested in it), the statements after it ending in a jump and
    holding no label, `break` or `continue`, has them put at the `break`, each goto a
    `break`, and a goto after the loop;
  - a label every goto to which ends a path through the one `if` before it holding them
    (statements after an `if` one arm of which ends in the goto and the other is empty
    moved into that other arm) goes, the gotos with it, and the statements between the `if`
    and the label are guarded by the negation of the paths' conditions (each `&&` of the
    path's tests, `||` of the paths, negated by De Morgan's laws); where statements lie
    between, each test must have no effect, store nothing, spell no `volatile` and read
    nothing the `if` changes after it;

  each where the cognitive complexity, counted as printed (`else if`), does not rise;
- *labelled pieces cut again* (`wayward_eagle.c.recut`, only with `recut`): a piece
  returning nothing left with a top-level label is reduced again, kept where that lowers
  both its gotos and its total cognitive complexity and adds no piece returning exit codes;
- a parameter of a `static` piece it never mentions goes, with its argument in each call of
  the piece, until none is left; a parameter of the original function it never reads is
  `(void)p;`.
