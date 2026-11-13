#!/bin/sh
# Copies the Kindle side onto a Kindle mounted as USB storage and writes its config.
#   ./install.sh <kindle mount> <server url> <token>
#   ./install.sh /Volumes/Kindle http://192.168.1.10:8100 0123abcd...
# Linux mounts are usually /media/$USER/Kindle. Eject the Kindle afterwards, then start
# "Server Dashboard" from the library.
set -e
[ $# -eq 3 ] || { sed -n '2,6p' "$0"; exit 1; }
KINDLE=$1; SERVER=${2%/}; TOKEN=$3
HERE=$(cd "$(dirname "$0")" && pwd)
[ -d "$KINDLE/documents" ] || { echo "$KINDLE/documents not found: is the Kindle mounted there?"; exit 1; }
curl -fsS -m 5 "$SERVER/health" > /dev/null || echo "warning: $SERVER/health did not answer from here"
mkdir -p "$KINDLE/serverdash"
cp "$HERE/serverdash/loop.sh" "$HERE/serverdash/stop.sh" "$HERE/serverdash/serverdash.conf" "$KINDLE/serverdash/"
cp "$HERE/documents/Server Dashboard.sh" "$HERE/documents/Server Dashboard Autostart.sh" "$KINDLE/documents/"
printf 'SERVER=%s\nTOKEN=%s\n' "$SERVER" "$TOKEN" > "$KINDLE/serverdash/config"
echo "Installed into $KINDLE. Eject it, then open \"Server Dashboard\" in the library."
