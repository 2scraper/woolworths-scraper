#!/bin/sh
# Give the browser a display, then run the scraper.
#
# WHY THIS FILE EXISTS AT ALL
# ---------------------------
# Every other image in this family runs its engine `--headless`, because a
# container has no display. That does not work on woolworths.com.au:
# measured 2026-09-16 from one datacentre address, four URLs each way,
# headful was served HTTP 200 4 of 4 and headless got Akamai's 403 4 of 4.
# So the container has to run a REAL browser, which means it has to have a
# display to give it.
#
# WHY NOT `xvfb-run`
# ------------------
# That was the first attempt and it hangs in this image. `xvfb-run -a` picks
# a free server number by trying them in turn, and inside a container that
# loop never settles — `xvfb-run -a /bin/echo hello` produced no output and
# had to be killed at 60s. (Before that it failed outright with "xauth
# command not found", which is why `xauth` is installed alongside `xvfb`.)
#
# Starting Xvfb ourselves on a fixed display is both simpler and quieter: one
# process, one known display, no retry loop, and the scraper's own exit code
# reaches the caller because we `exec`.
#
# Found by RUNNING the image rather than by building it. The build was green
# for both of the broken versions above, which is why CI does both
# (CLAUDE.md §11).
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp >/dev/null 2>&1 &
XVFB_PID=$!

# Wait for the display to actually accept connections rather than sleeping a
# guessed interval: a fixed `sleep 1` is a race that passes on a quiet
# machine and fails on a loaded one.
i=0
while [ "$i" -lt 50 ]; do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 0.1
done

# Not fatal if it never came up: `--help` and a `--version`-style invocation
# need no display at all, and failing them because the X server was slow
# would be worse than letting the browser complain for itself.
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "warning: Xvfb did not come up on $DISPLAY; a browser launch will fail" >&2
fi

trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT INT TERM

exec python3 playwright_scraper.py "$@"
