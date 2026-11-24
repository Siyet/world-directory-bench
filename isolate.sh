#!/bin/sh
# Host isolation for series 10 (runtime only; repeat after a reboot) on a 6-vCPU VM: CPUs 0-3 for the
# benchmark containers, 4-5 for everything else.
set -u
for t in apt-daily apt-daily-upgrade fwupd-refresh motd-news update-notifier-download man-db fstrim dpkg-db-backup e2scrub_all systemd-tmpfiles-clean sysstat-collect sysstat-summary logrotate ua-timer update-notifier-motd apport-autoreport snapd.snap-repair xfs_scrub_all; do systemctl stop $t.timer 2>/dev/null; done
units="docker.service containerd.service user.slice init.scope fwupd.service packagekit.service systemd-hostnamed.service systemd-timedated.service systemd-localed.service"
units="$units $(systemctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}')"
units="$units $(systemctl list-timers --all --no-legend --plain | awk '{print $NF}' | grep '\.service$')"
for u in $(echo $units | tr ' ' '\n' | sort -u); do systemctl set-property --runtime "$u" AllowedCPUs=4-5 || echo "failed $u"; done
echo 30 > /proc/irq/default_smp_affinity
echo 30 > /sys/devices/virtual/workqueue/cpumask
for d in /proc/irq/*/; do n=$(basename $d); [ -f $d/smp_affinity_list ] && echo 4-5 > $d/smp_affinity_list 2>/dev/null; done
iface=$(awk '$2=="00000000" && $8=="00000000"{print $1}' /proc/net/route | sort -u | tr '\n' ' ' | sed 's/ $//')
[ "$(echo "$iface" | wc -w)" -eq 1 ] || { echo "expected one IPv4 default route interface: '$iface'"; exit 1; }
nic=$(basename "$(readlink -f /sys/class/net/$iface/device)")
case "$nic" in virtio*) ;; *) echo "default route interface $iface is not virtio: $nic"; exit 1;; esac
echo "NIC $iface -> $nic"
grep -E "$nic-" /proc/interrupts | cut -d: -f1 | while read n; do echo "$n $(cat /proc/irq/$n/effective_affinity_list)"; done
