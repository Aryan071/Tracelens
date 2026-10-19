#!/usr/bin/env bash
# One line describing the conditions a benchmark ran under. Absolute numbers depend on them: on a laptop
# the same code runs about twice as fast plugged in as on battery with the powersave governor.
cpu=$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | xargs)
gov=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "?")
mhz=$(awk -F: '/MHz/ {s+=$2; n++} END {printf "%.0f", s/n}' /proc/cpuinfo)
src="mains"; for b in /sys/class/power_supply/BAT*/status; do [ -r "$b" ] && [ "$(cat "$b")" = Discharging ] && src="BATTERY"; done
echo "env: $cpu | governor $gov | mean core clock ${mhz} MHz | power: $src | load $(cut -d' ' -f1 /proc/loadavg)"
