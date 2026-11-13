#!/bin/sh
# Name: Server Dashboard
# Author: kindle-homelab-dashboard
# DontUseFBInk
# Starts the homelab dashboard (/mnt/us/serverdash). Exit with its Exit button or 5 quick taps.
DIR=/mnt/us/serverdash
echo "$(date '+%F %T') scriptlet run" >> "$DIR/log.txt"
sh "$DIR/stop.sh" quiet
cd "$DIR" || exit 1
if command -v setsid > /dev/null; then
    setsid sh "$DIR/loop.sh" >> "$DIR/log.txt" 2>&1 < /dev/null &
else
    nohup sh "$DIR/loop.sh" >> "$DIR/log.txt" 2>&1 < /dev/null &
fi
