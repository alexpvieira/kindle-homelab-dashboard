#!/bin/sh
# Kindle side of kindle-homelab-dashboard (see its README).
# The Kindle is only a touch screen: it shows the PNG the server renders and sends every tap's raw
# coordinates back (GET /k/screen?x=&y=); the server owns layout, state and actions.
# The Kindle UI (cvm) is paused while this runs. Ways out: the Exit button (Actions tab), or 5 quick taps
# anywhere (works even when the server is down). Log: /mnt/us/serverdash/log.txt
# Runs from a copy in /var/tmp: /mnt/us disappears while the Kindle is in USB storage mode. Then the UI is
# resumed (so the Kindle shows its USB screen) and paused again once the storage is back.
DIR=/mnt/us/serverdash
RUN=/var/tmp/serverdash
if [ "$0" != "$RUN/loop.sh" ]; then
    mkdir -p "$RUN" && cp "$DIR/loop.sh" "$DIR/stop.sh" "$DIR/config" "$RUN/" && exec sh "$RUN/loop.sh"
    echo "can't copy to $RUN"; exit 1
fi
. "$RUN/config"                 # SERVER=http://host:port, TOKEN=...
POLL=20                         # seconds between checks for a new screen while idle
FAST=3                          # ... for a minute after a tap (shows action results quickly)
FBINK=/mnt/us/libkh/bin/fbink
UI_PROCS="cvm KPPMainApp"     # processes paused while the dashboard shows (stop.sh resumes them)

log() {  # to the run dir, and to the USB-visible log while /mnt/us is there
    line="$(date '+%F %T') $*"
    echo "$line" >> "$RUN/log.txt"
    [ -d "$DIR" ] && echo "$line" >> "$DIR/log.txt"
}

pause_ui() {  # $1 = STOP or CONT. The Kindle UI: cvm (Java framework) and the KPP home/library app.
    for ui in $UI_PROCS; do
        for pid in $(pidof "$ui"); do kill -"$1" "$pid" && log "$1 $ui ($pid)"; done
    done
}

touch_device() {  # the input device that reports multi-touch positions
    for h in $(awk '/^N: Name=/ { n = tolower($0) } /^H: Handlers=/ && n ~ /touch|goodix|cyttsp|pt_mt|zforce|elan|[-_]ts"/ { if (match($0, /event[0-9]+/)) print substr($0, RSTART, RLENGTH) }' /proc/bus/input/devices); do
        echo "/dev/input/$h"; return
    done
    echo /dev/input/event2
}

show() {  # $1 = PNG, $2 = 1 for a full (flashing) refresh
    if [ -n "$USE_FBINK" ]; then
        if [ "$2" = 1 ]; then "$FBINK" -q -f -g file="$1"; else "$FBINK" -q -g file="$1"; fi
    else
        if [ "$2" = 1 ]; then eips -f -g "$1"; else eips -g "$1"; fi
    fi > /dev/null 2>&1
}

message() {  # plain text on a cleared screen
    if [ -n "$USE_FBINK" ]; then
        "$FBINK" -q -c -f
        "$FBINK" -q -m -y 20 -S 3 -- "$1"
        "$FBINK" -q -m -y 26 -S 2 -- "$2"
    else
        eips -c; eips 2 20 "$1"; eips 2 23 "$2"
    fi > /dev/null 2>&1
}

# Reads raw touch events (16 bytes each: sec sec usec usec type code value value as 16-bit words)
# and writes "x y" to $RUN/tap once per touch. Five touches within 4 s run stop.sh.
watch_touch() {
    x=-1; y=-1; down=0; sent=1; seen_down=0; moved=0; last=0; t1=0; t2=0; t3=0; t4=0
    while :; do
        set -- $(dd bs=16 count=1 2>/dev/null <&3 | od -An -t d2)
        [ $# -ge 8 ] || { sleep 1; continue; }
        case "$5:$6" in
        3:53|3:0) x=$7; moved=1 ;;
        3:54|3:1) y=$7; moved=1 ;;
        3:57) if [ "$7" -ge 0 ]; then down=1; sent=0; seen_down=1; else down=0; fi ;;
        1:330) if [ "$7" = 1 ]; then down=1; sent=0; seen_down=1; else down=0; fi ;;
        0:0)
            fire=0
            if [ "$seen_down" = 1 ]; then
                [ "$down" = 1 ] && [ "$sent" = 0 ] && [ "$x" -ge 0 ] && [ "$y" -ge 0 ] && fire=1
            elif [ "$moved" = 1 ] && [ $(($(date +%s) - last)) -ge 1 ]; then
                fire=1
            fi
            moved=0
            if [ "$fire" = 1 ]; then
                sent=1; now=$(date +%s); last=$now
                echo "$x $y" > "$RUN/tap"
                log "touch raw x=$x y=$y"
                t1=$t2; t2=$t3; t3=$t4; t4=$now
                if [ $((now - t1)) -le 4 ] && [ "$t1" -gt 0 ]; then
                    log "5 quick taps: exit"
                    sh "$RUN/stop.sh"; exit 0
                fi
            fi ;;
        esac
    done
}

[ -f "$DIR/log.txt" ] && [ "$(wc -c < "$DIR/log.txt")" -gt 300000 ] && mv -f "$DIR/log.txt" "$DIR/log.old"
log "starting as $(id) from $RUN"
"$FBINK" -e > "$RUN/fbink.txt" 2>&1 && USE_FBINK=1
log "fbink: ${USE_FBINK:-no, using eips ($(tail -1 "$RUN/fbink.txt"))}"
TOUCH=$(touch_device)
log "touch device: $TOUCH"
# Whatever ends this loop, never leave the Kindle UI paused.
trap 'pause_ui CONT; exit 0' EXIT INT TERM HUP

lipc-set-prop com.lab126.cmd wirelessEnable 1 2>/dev/null
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
sleep 3   # let the library close
if [ -c "$TOUCH" ] && [ -r "$TOUCH" ] && exec 3< "$TOUCH"; then
    pause_ui STOP; paused=1
    watch_touch &
else
    log "can't read $TOUCH: no touch, Kindle UI left running"
fi

VER=""; last_fetch=0; last_tap=0; fails=0
rm -f "$RUN/tap" "$RUN/tap.read"
while :; do
    now=$(date +%s); q=""
    # USB storage mode: /mnt/us is gone. Hand the screen back until it returns.
    if [ ! -f "$DIR/config" ]; then
        [ "$paused" = 1 ] && { log "USB storage mode: resuming the Kindle UI"; pause_ui CONT; paused=2; }
        sleep 3; continue
    fi
    if [ "$paused" = 2 ]; then
        log "storage back: pausing the Kindle UI again"
        sleep 5; pause_ui STOP; paused=1; VER=""; rm -f "$RUN/tap"
    fi
    if mv -f "$RUN/tap" "$RUN/tap.read" 2>/dev/null; then
        set -- $(cat "$RUN/tap.read"); q="&x=$1&y=$2"; last_tap=$now
    fi
    interval=$POLL; [ $((now - last_tap)) -lt 60 ] && interval=$FAST
    if [ -n "$q" ] || [ $((now - last_fetch)) -ge "$interval" ]; then
        last_fetch=$now
        # Battery level and charging state, so the dashboard can warn when the cable comes loose.
        batt=$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)
        chg=$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null)
        code=$(curl -s -m 30 -o "$RUN/new.png" -D "$RUN/hdr" -w '%{http_code}' "$SERVER/k/screen?t=$TOKEN&v=$VER$q&b=$batt&c=$chg")
        action=$(grep -i '^x-action:' "$RUN/hdr" 2>/dev/null | tr -d '\r' | awk '{ print $2 }')
        if [ "$action" = exit ]; then
            log "exit from server"; sh "$RUN/stop.sh"; exit 0
        elif [ "$code" = 200 ]; then
            flash=0; [ "$action" = flash ] || [ "$fails" -gt 0 ] && flash=1
            show "$RUN/new.png" "$flash"
            VER=$(md5sum "$RUN/new.png" | cut -d' ' -f1); fails=0
        elif [ "$code" != 204 ]; then
            fails=$((fails + 1))
            log "fetch failed ($fails): http $code"
            # Wi-Fi may have dropped: nudge it. Tell the user after a few misses.
            [ "$fails" = 2 ] && lipc-set-prop com.lab126.cmd wirelessEnable 1 2>/dev/null
            [ "$fails" = 3 ] && { message "Dashboard server not reachable" "Retrying. Tap 5 times quickly to exit."; VER=""; }
        fi
    fi
    usleep 250000 2>/dev/null || sleep 1
done
