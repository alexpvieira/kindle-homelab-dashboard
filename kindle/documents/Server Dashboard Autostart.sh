#!/bin/sh
# Name: Server Dashboard Autostart
# Author: kindle-homelab-dashboard
# DontUseFBInk
# Toggles starting the homelab dashboard by itself 90 s after every boot. The boot job is
# /etc/upstart/serverdash.conf; it only acts while /mnt/us/serverdash/autostart exists.
DIR=/mnt/us/serverdash
log() { echo "$(date '+%F %T') autostart: $*" >> "$DIR/log.txt"; }
if [ -f "$DIR/autostart" ]; then
    rm -f "$DIR/autostart"
    msg="Server Dashboard autostart is OFF"
else
    if ! cmp -s "$DIR/serverdash.conf" /etc/upstart/serverdash.conf; then
        mntroot rw 2>/dev/null || mount -o remount,rw /
        cp "$DIR/serverdash.conf" /etc/upstart/serverdash.conf
        sync
        mntroot ro 2>/dev/null || mount -o remount,ro /
    fi
    if cmp -s "$DIR/serverdash.conf" /etc/upstart/serverdash.conf; then
        touch "$DIR/autostart"
        msg="Server Dashboard autostart is ON (starts 90 s after boot)"
    else
        msg="Autostart FAILED: could not install the boot job"
    fi
fi
log "$msg"
eips 1 3 "$msg" > /dev/null 2>&1
