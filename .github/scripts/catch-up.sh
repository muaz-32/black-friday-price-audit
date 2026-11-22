#!/usr/bin/env bash
# Fold into a state history any snapshot that an earlier run left out, so the
# history is complete before this run's snapshot ($RUN) is added to it.
#
#   .github/scripts/catch-up.sh variants state/variant-history-<run>.parquet
#   .github/scripts/catch-up.sh products state/product-history-<run>.parquet
#
# (Without the catalog file: product titles of a missed run are filled in by
# the next full-catalog run instead.)
#
# The histories are change logs that only take snapshots in order: once a
# later snapshot is in, an earlier one is refused. So a run whose update step
# failed would be missing from them for good unless the next run puts it back
# first. The snapshots themselves are in the monthly data releases. If this
# fails, the step fails before $RUN is added, and the next run tries again.
set -euo pipefail
module=$1
newest=${2:-}
[ -n "$newest" ] || exit 0 # no history yet: nothing to catch up on
last=$(basename "$newest" .parquet)
last=${last##*-}

printf 'data-%s-%s\n' "${last:0:4}" "${last:4:2}" "${RUN:0:4}" "${RUN:4:2}" | sort -u |
  while read -r tag; do gh release view "$tag" --json assets --jq '.assets[].name' 2>/dev/null || true; done |
  sed -n 's/^runs-\(.*\)\.parquet$/\1/p' | sort -u >missed-candidates.txt

while read -r run; do
  if [[ "$run" > "$last" && "$run" < "$RUN" ]]; then
    echo "catching up: $run is in the data release but not in $(basename "$newest")"
    gh release download "data-${run:0:4}-${run:4:2}" --dir data --clobber \
      -p "prices-$run.parquet" -p "runs-$run.parquet"
    python -m "collector.$module" --run "$run"
    rm -f "data/prices-$run.parquet"
  fi
done <missed-candidates.txt
rm -f missed-candidates.txt
