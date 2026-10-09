#!/bin/sh
# Runs in the booted target: report the Secure Boot state of the final OS, then power off.
exec > /dev/ttyS0 2>&1
echo "=== TARGET BEGIN"
echo "kernel: $(uname -r)"
echo "sb-state: $(mokutil --sb-state 2>&1)"
echo "lockdown: $(cat /sys/kernel/security/lockdown)"
echo "root: $(findmnt -no SOURCE,FSTYPE /)"
echo "zfs loaded: $(grep -c '^zfs ' /proc/modules)"
echo "zfs signer: $(modinfo -F signer zfs)"
echo "machine keyring: $(keyctl show %:.machine 2>&1 | tail -n +2 | sed 's/.*asymmetric: //' | tr '\n' ';')"
echo "=== TARGET END"
systemctl poweroff
