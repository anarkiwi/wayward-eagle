#!/bin/sh
# Lift gravity-inversion through a deity-informant checkout (DI=DIR) once per combination of
# this checkout's optional rewrites made default (carries, recut, per-value copies always), into
# OUT/<combination> (LIFT=gi_lift_path.sh to lift in place), census JSON beside each:
# gi_combos.sh GI_DIR OUT [COMBINATION...]
set -e
GI=$1; OUT=$(realpath -m "$2"); shift 2
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-$HERE/out/venv/bin}
for combo in ${*:-carries0-recut0-copies0 carries1-recut0-copies0 carries0-recut1-copies0 carries1-recut1-copies0 carries0-recut0-copies1 carries1-recut0-copies1 carries0-recut1-copies1 carries1-recut1-copies1}; do
  WE=$OUT/we-$combo; rm -rf "$WE"; mkdir -p "$WE"
  (cd "$HERE" && tar --exclude=./out --exclude=./.claude --exclude=./.git -cf - . | tar -xf - -C "$WE")
  case $combo in *carries1*) sed -i '0,/    carries=False,/s//    carries=True,/' "$WE/wayward_eagle/c/reduce.py";; esac
  case $combo in *recut1*) sed -i '0,/    recut=False,/s//    recut=True,/' "$WE/wayward_eagle/c/reduce.py";; esac
  case $combo in *copies1*) sed -i 's/^    return _complexity(new) < _complexity(old) and _accesses(new) <= _accesses(old)$/    return True/' "$WE/wayward_eagle/c/consts.py";; esac
  echo "== $combo"
  WE=$WE "$HERE/tools/${LIFT:-gi_lift.sh}" "$GI" "$OUT/$combo" >/dev/null 2>&1
  (cd "$OUT/$combo" && "$PY/wayward-eagle" census --json -j 2 -I . -I "$GI/c64" *.c > "$OUT/$combo.json" 2>/dev/null)
done
