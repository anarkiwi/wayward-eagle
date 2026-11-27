#!/bin/sh
# Run gravity-inversion's per-tune SID-write check in its deps image, with this checkout of
# wayward-eagle installed (WE=DIR another; DI=DIR a deity-informant checkout's Python over the
# image's; PYPATH=DIR:.. after GI's tools on PYTHONPATH, /scratch mounted; BASE=1 the image's
# own wayward-eagle; IMAGE the image):
# gi_check.sh GI_DIR [TUNE...]
set -e
GI=$1; shift
WE=${WE:-$(cd "$(dirname "$0")/.." && pwd)}
docker run --rm -v "$GI":/src:ro -v "$WE":/we:ro -v "${DI:-$WE}":/di:ro -e DI="${DI:-}" -e HOME=/tmp -e BASE="${BASE:-}" \
  ${PYPATH:+-v /scratch:/scratch:ro -e PYTHONPATH=/work/tools:$PYPATH} \
  "${IMAGE:-gi-deps-main:latest}" bash -c '
  mkdir -p /tmp/we /work
  (cd /src && tar --exclude=./.claude --exclude=./out --exclude=./.git -cf - . | tar -xf - -C /work)
  if [ -z "$BASE" ]; then
    (cd /we && tar --exclude=./out --exclude=./.claude -cf - . | tar -xf - -C /tmp/we)
    pip install -q --no-deps /tmp/we >/dev/null 2>&1
  fi
  if [ -n "$DI" ]; then
    site=$(python3 -c "import deity_informant, os; print(os.path.dirname(deity_informant.__path__[0]))")
    (cd /di && find deity_informant -name "*.py" -print0 | tar --null -T - -cf -) | tar -xf - -C "$site"
  fi
  python3 -c "import wayward_eagle, importlib.metadata as m; print(\"wayward-eagle\", m.version(\"wayward-eagle\"))"
  cd /work
  for t in ${*:-$(ls tunes)}; do
    printf "%s: " "$t"; make -s -C tunes/$t check 2>&1 | grep -E "OK|MISMATCH|rror" | tr "\n" " "
    printf "prg %s bytes\n" "$(stat -c %s tunes/$t/build/gen.prg 2>/dev/null)"
  done' sh "$@"
