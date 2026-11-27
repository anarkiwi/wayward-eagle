#!/bin/sh
# Write gravity-inversion's readability C to OUT, run in place in GI_DIR (under /scratch) with
# this checkout (WE=DIR another) then PYPATH's entries (a lifter's checkout) first on PYTHONPATH:
# gi_lift_path.sh GI_DIR OUT [TARGET...]
set -e
GI=$1; OUT=$(realpath -m "$2"); shift 2
WE=${WE:-$(cd "$(dirname "$0")/.." && pwd)}
mkdir -p "$OUT"
P=$GI/tools:$WE${PYPATH:+:$PYPATH}
docker run --rm --init -u "$(id -u):$(id -g)" -e HOME=/tmp -e OMP_NUM_THREADS=2 \
  -e MKL_NUM_THREADS=2 -e OPENBLAS_NUM_THREADS=2 -e PYTHONPATH="$P" -v /scratch:/scratch \
  -w "$GI" "${IMAGE:-gi-deps-main:latest}" python3 tools/readability.py -j 8 --out "$OUT" "$@"
