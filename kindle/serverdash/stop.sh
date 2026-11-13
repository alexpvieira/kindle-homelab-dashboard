#!/bin/sh
# Stops the homelab dashboard and gives the screen back to the Kindle. "stop.sh quiet" only kills it.
DIR=/mnt/us/serverdash
for p in /proc/[0-9]*; do
    [ "${p#/proc/}" = $$ ] && continue
    grep -q 'serverdash/loop.sh' "$p/cmdline" 2>/dev/null && kill "${p#/proc/}" 2>/dev/null
done
rm -f /var/tmp/serverdash/tap /var/tmp/serverdash/tap.read
for ui in cvm KPPMainApp; do for pid in $(pidof "$ui"); do kill -CONT "$pid"; done; done
[ "$1" = quiet ] && exit 0
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
lipc-set-prop com.lab126.appmgrd start app://com.lab126.booklet.home 2>/dev/null
