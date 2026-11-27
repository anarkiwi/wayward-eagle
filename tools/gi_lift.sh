#!/bin/sh
# Write gravity-inversion's readability C (tools/readability.py) to OUT in its deps image, with
# this checkout of wayward-eagle installed (WE=DIR another checkout; DI=DIR a deity-informant checkout's Python over the image's; IMAGE the image):
# gi_lift.sh GI_DIR OUT [TARGET...]
set -e
GI=$1; OUT=$(realpath -m "$2"); shift 2
WE=${WE:-$(cd "$(dirname "$0")/.." && pwd)}
mkdir -p "$OUT"
docker run --rm -v "$GI":/src:ro -v "$WE":/we:ro -v "${DI:-$WE}":/di:ro -e DI="${DI:-}" -v "$OUT":/out -e HOME=/tmp \
  "${IMAGE:-gi-deps-main:latest}" bash -c '
  mkdir -p /tmp/we
  (cd /we && tar --exclude=./out --exclude=./.claude -cf - . | tar -xf - -C /tmp/we)
  pip install -q --no-deps /tmp/we >/dev/null 2>&1
  if [ -n "$DI" ]; then
    site=$(python3 -c "import deity_informant, os; print(os.path.dirname(deity_informant.__path__[0]))")
    (cd /di && find deity_informant -name "*.py" -print0 | tar --null -T - -cf -) | tar -xf - -C "$site"
  fi
  cd /src && python3 tools/readability.py --out /out "$@"' sh "$@"
