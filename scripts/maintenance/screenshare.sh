#!/bin/bash
# Enable or disable macOS Screen Sharing on demand. The secure default is off.

set -euo pipefail

[ "$(id -u)" -eq 0 ] || {
    echo "screenshare: must run as root (use sudo)" >&2
    exit 1
}

usage() {
    echo "usage: screenshare on|off" >&2
    exit 2
}

case "${1:-}" in
    on)
        launchctl enable system/com.apple.screensharing
        launchctl bootstrap system \
            /System/Library/LaunchDaemons/com.apple.screensharing.plist \
            2>/dev/null || true
        sleep 1
        if lsof -nP -iTCP:5900 -sTCP:LISTEN >/dev/null 2>&1; then
            echo "Screen Sharing ON - close it with: sudo /usr/local/sbin/screenshare off"
        else
            echo "screenshare: enabled but :5900 is not listening" >&2
            exit 1
        fi
        ;;
    off)
        launchctl bootout system/com.apple.screensharing 2>/dev/null || true
        launchctl disable system/com.apple.screensharing
        sleep 1
        if lsof -nP -iTCP:5900 -sTCP:LISTEN >/dev/null 2>&1; then
            echo "screenshare: :5900 is still listening" >&2
            exit 1
        fi
        echo "Screen Sharing OFF - :5900 closed"
        ;;
    *) usage ;;
esac
