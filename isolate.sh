#!/bin/sh
# Host isolation for series 8 (runtime only; repeat after a reboot) on an 8-vCPU VM with two NUMA
# nodes: CPUs 0-3 (node 0) for the benchmark containers, 4-7 (node 1) for everything else.
set -u
for t in apt-daily apt-daily-upgrade dpkg-db-backup man-db e2scrub_all fstrim systemd-tmpfiles-clean sysstat-collect sysstat-summary; do systemctl stop $t.timer 2>/dev/null; done
units="docker.service containerd.service user.slice init.scope"
units="$units $(systemctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}')"
units="$units $(systemctl list-timers --all --no-legend --plain | awk '{print $NF}' | grep '\.service$')"
for u in $(echo $units | tr ' ' '\n' | sort -u); do systemctl set-property --runtime "$u" AllowedCPUs=4-7 || echo "failed $u"; done
echo f0 > /proc/irq/default_smp_affinity
echo f0 > /sys/devices/virtual/workqueue/cpumask
for d in /proc/irq/*/; do n=$(basename $d); [ -f $d/smp_affinity_list ] && echo 4-7 > $d/smp_affinity_list 2>/dev/null; done
grep -E 'virtio0-' /proc/interrupts | cut -d: -f1 | while read n; do echo "$n $(cat /proc/irq/$n/effective_affinity_list)"; done
