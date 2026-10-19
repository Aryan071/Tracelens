#!/usr/bin/env bash
# Run a command only when nothing else is using the CPU, and say whether it stayed that way.
#   bench/with_quiet_machine.sh <command...>
# Benchmarks on a shared laptop are meaningless while another job runs: this waits until no process
# outside Docker/this repo's tools uses more than 40% of a core for 15 s in a row, then runs the
# command while sampling the busiest foreign process once a second. The verdict is printed after.
set -uo pipefail
# a foreign job: a busy process that is neither inside a container (our benchmarks run in Docker) nor ours
busy() {
  ps -eo pid,pcpu,args --sort=-pcpu | awk 'NR>1 && $2>40 {print $1" "$2" "$3}' | while read -r pid cpu cmd; do
    grep -q docker "/proc/$pid/cgroup" 2>/dev/null && continue
    case "$cmd" in *ps|*awk|*with_quiet*|*bench/*|*clickhouse*|*dockerd*|*containerd*) continue;; esac
    echo "$cpu $cmd"; break
  done
}
"$(dirname "$0")/env.sh"
quiet=0
for _ in $(seq 1 360); do   # up to 30 minutes
  if [ -z "$(busy)" ]; then quiet=$((quiet+1)); else quiet=0; fi
  [ $quiet -ge 3 ] && break
  sleep 5
done
[ $quiet -ge 3 ] || { echo "machine never became quiet; not running" >&2; exit 3; }
S=$(mktemp); ( while true; do busy >> "$S"; sleep 1; done ) & SAMPLER=$!
"$@"; rc=$?
kill $SAMPLER 2>/dev/null
if [ -s "$S" ]; then echo "NOT CLEAN: foreign CPU load during the run ($(wc -l < "$S") samples), e.g. $(sort -rn "$S" | head -1)"; else echo "CLEAN: no foreign process above 40% of a core during the run"; fi
rm -f "$S"; exit $rc
