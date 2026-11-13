# Notes for AI coding agents

This repository is a Kindle touch-screen dashboard for a home server: `server/` (Python, Docker) renders
PNG pages and runs actions, `kindle/` (shell) shows them on a jailbroken Kindle and sends taps back.
Read README.md first.

## Setting it up for a user

Work through these steps, asking the user only where noted. Never expose port 8100 outside the LAN.

1. **Server access.** You need a shell on the server that will run the dashboard (local or `ssh`), with
   Docker and Docker Compose. Check: `docker version`, `docker compose version`.
2. **Copy `server/`** to the server (for example `~/kindle-dash` or wherever the user keeps compose
   stacks) and create `config.toml` from `config.example.toml`. Generate the token with
   `openssl rand -hex 16`. Keep `config.toml` readable only by its owner (`chmod 600`).
3. **Discover what to show** and fill in `config.toml`:
   - Disks: `df -h -x tmpfs -x devtmpfs -x overlay` and `findmnt -t nfs,nfs4,cifs,smb3`. The root
     filesystem is `/rootfs` inside the container. Every other disk or share needs a volume in
     `compose.yaml` (read-only; `rslave` for automounts) and a `[[storage]]` row with a short `detail`
     (what lives there). Three rows fit best.
   - UPS: is NUT running? `upsc -l` or a listener on port 3493 (`ss -ltn | grep 3493`). If so add `[ups]`
     with the UPS name from `upsc -l`.
   - Gatus: a container from the `twinproject/gatus` image; add `[gatus]` with its URL as seen from the
     host (the dashboard uses host networking).
   - Network interface: the default (default route) is usually right.
   - Containers: `docker ps --format '{{.Names}}'`. **Ask the user** which (up to 4) they restart most
     often; those become `shortcuts`.
   - Actions: **ask the user** what they want as buttons (up to 4). Keep `restart_unhealthy`; offer
     `reboot_host` only on a systemd host. Smart plugs (Shelly, Tasmota), router reboot endpoints and
     scripts in containers map to `http` / `exec` actions. Write the confirm `text` so it says what goes
     down and for how long.
   - The Kindle model: **ask** if not known; set `screen` for anything other than a Paperwhite 3/4 or Voyage.
4. **Start it**: `docker compose up -d --build`, then `docker logs kindle-dash` (expect
   `kindle-dash on :8100 ...`) and fetch `http://localhost:8100/preview.png?t=<token>` plus
   `&page=containers` and `&page=actions`. Look at the images: text must not overlap or run off a card.
5. **The Kindle**: ask the user to connect it by USB in storage mode. Find its mount (`/Volumes/Kindle` on
   macOS, `/media/$USER/Kindle` on Linux) and run
   `kindle/install.sh <mount> http://<server LAN IP>:8100 <token>`. Use the server's LAN IP, not a
   hostname the Kindle may not resolve. Then the user ejects it and opens **Server Dashboard** from the
   library (and **Server Dashboard Autostart** once, if they want it to start on boot).
6. **Check taps**: ask the user to tap the four corners; `docker logs kindle-dash` shows
   `tap x,y -> action`. The corners should be near (0,0), (1071,0), (0,1447), (1071,1447) in portrait.
   If not, set `[touch]` (swap / flip / max).

## Changing the code

- `server/app.py` is a single file, in this order: config, data sources (`collect()`), actions, drawing
  helpers (`Canvas`), page functions (`page_status`, `page_containers`, `page_actions`, `page_confirm`),
  the tap state machine (`handle_tap`) and the HTTP handler. Keep it stdlib + Pillow.
- Layout is in pixels for a 1072-wide portrait canvas (`W`, `H`; `CL`/`CR`/`CB` are the content edges).
  Landscape is drawn at 1448 × 1072 and rotated; always check both orientations.
- Tappable areas are `cv.zone(box, action)` (buttons add theirs). The first zone containing a tap wins.
- Icons are Font Awesome glyphs in the `I` dict. **The dict holds the literal private-use characters**,
  so search-and-replace on `\uf...` escapes will not match; edit with a script that uses the characters.
- E-ink: black/white/four greys (`BLACK DARK MID LIGHT PALE WHITE`). Avoid anything that changes every
  render (seconds, countdowns): the Kindle only redraws when the PNG changes, and needless redraws flash.
- Test with demo data: `DEMO=1` (no Docker socket needed). Render every page in both orientations and look
  at them before you finish:

  ```sh
  docker build -t khd server && docker run --rm -e DEMO=1 -p 8100:8100 khd
  # http://localhost:8100/preview.png?t=demo&page=status   (rotate: tap Rotate, or set orientation in /state)
  ```
- Kindle scripts run under the Kindle's busybox `sh` as root: no bash-isms. `loop.sh` copies itself to
  `/var/tmp/serverdash` because `/mnt/us` disappears in USB storage mode. Whatever ends the loop must
  resume the Kindle UI processes (`cvm`, `KPPMainApp`), or the Kindle looks frozen.

