#!/bin/sh
# Build a tune's PRG from given C with gravity-inversion's rules and compare its SID writes
# with the tune's PSID (its check-gen target): sid_gate.sh GI_ROOT TUNE C_FILE H_FILE OUTDIR
set -eu
gi=$1 tune=$2 c=$3 h=$4 b=$5
mkdir -p "$b/gen"
cp "$c" "$b/gen/$tune.c"
cp "$h" "$b/gen/$tune.h"
cp "$gi/tunes/$tune/build/gen/cgen_trap.c" "$b/gen/cgen_trap.c"
for f in ref.sid ref.labels; do
  [ -f "$gi/tunes/$tune/build/$f" ] && cp "$gi/tunes/$tune/build/$f" "$b/$f"
done
touch "$b/lifter.stamp"
rm -f "$b/gen.prg" "$b/gen.prg.elf"
make -s -C "$gi/tunes/$tune" B="$b" -o "$b/gen/$tune.c" -o "$b/gen/$tune.h" \
  -o "$b/gen/cgen_trap.c" -o "$b/lifter.stamp" -o "$b/ref.sid" -o "$b/ref.labels" check-gen
