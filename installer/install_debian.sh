#!/usr/bin/env bash
# Install or upgrade RDR on Debian 12 or newer: an LXC container, a VM or a PC.
#
# Run as root from the unpacked rdr-debian-<version>.tar.gz:
#     bash installer/install_debian.sh
#
# The app goes to /opt/rdr and the data (database, backups, settings) stays in
# /var/lib/rdr. A systemd service named "rdr" runs it as its own user and
# starts it on boot. Running the script again from a newer download upgrades
# the app and never touches the data.
#
# Set RDR_SKIP_FFMPEG=1 to leave ffmpeg out (only the mirror's programme feed needs it).
set -euo pipefail
# "su" without a login shell leaves the admin tools off the PATH on Debian.
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"

APP_DIR=/opt/rdr
DATA_DIR=/var/lib/rdr
SVC_USER=rdr
UNIT=/etc/systemd/system/rdr.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "!! $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run this as root (or with sudo)."
[ -f "$SRC/run.py" ] && [ -d "$SRC/app" ] && [ -f "$SRC/requirements.txt" ] \
    || die "Run this from the unpacked RDR download: $SRC has no app to install."
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
    *" debian "*) ;;
    *) die "This installer is for Debian and systems based on it." ;;
esac

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/app/__init__.py")"
echo "== Installing RDR $VERSION"

echo "== Packages"
export DEBIAN_FRONTEND=noninteractive
PACKAGES="python3 python3-venv ca-certificates"
[ "${RDR_SKIP_FFMPEG:-0}" = 1 ] || PACKAGES="$PACKAGES ffmpeg"
apt-get update -q
# shellcheck disable=SC2086
apt-get install -y -q --no-install-recommends $PACKAGES
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "RDR needs Python 3.11 or newer (Debian 12 or newer). Found $(python3 --version 2>&1)."

echo "== User and folders"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$DATA_DIR"
install -d -m 755 "$APP_DIR"

HAVE_SYSTEMD=0
[ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 && HAVE_SYSTEMD=1
if [ "$HAVE_SYSTEMD" = 1 ] && systemctl is-active --quiet rdr; then
    echo "== Stopping the running service"
    systemctl stop rdr
fi

echo "== App files"
if [ "$SRC" != "$APP_DIR" ]; then
    rm -rf "$APP_DIR/app"
    cp -r "$SRC/app" "$APP_DIR/app"
    for f in run.py tray.py requirements.txt README.md; do
        [ ! -f "$SRC/$f" ] || cp "$SRC/$f" "$APP_DIR/$f"
    done
fi
find "$APP_DIR/app" -type d -name __pycache__ -prune -exec rm -rf {} +
printf '%s' "$VERSION" > "$APP_DIR/VERSION"

echo "== Python environment"
if ! "$APP_DIR/.venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
    python3 -m venv --clear "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install -q --disable-pip-version-check --no-cache-dir -r "$APP_DIR/requirements.txt"
# The service user owns the app so "Update and restart" in Admin can replace it.
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$DATA_DIR"

# Where the server listens, from the saved settings (a fresh install uses port 8080 on every address).
LISTEN="$(runuser -u "$SVC_USER" -- env RDR_DATA_DIR="$DATA_DIR" "$APP_DIR/.venv/bin/python" -c \
    'import sys; sys.path.insert(0, sys.argv[1]); from app.config import load_config; c = load_config(); print(c["host"], c["port"])' "$APP_DIR")"
HOST="${LISTEN% *}"
PORT="${LISTEN##* }"
case "$HOST" in 0.0.0.0|::|"") HOST=127.0.0.1 ;; esac
START_CMD="runuser -u $SVC_USER -- env RDR_DATA_DIR=$DATA_DIR $APP_DIR/.venv/bin/python $APP_DIR/run.py --no-browser"

if [ "$HAVE_SYSTEMD" != 1 ]; then
    echo "== Installed. systemd is not running here, so no service was set up."
    echo "   Start RDR with:"
    echo "   $START_CMD"
    exit 0
fi

echo "== Service"
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=RDR Radio Distribution Room
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR
Environment=RDR_DATA_DIR=$DATA_DIR
Environment=PIP_NO_CACHE_DIR=1
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py --no-browser
# run.py gets the stop signal and shuts its web server down cleanly.
KillMode=mixed
TimeoutStopSec=30
Restart=always
RestartSec=3
NoNewPrivileges=yes
# Lets the server use port 80 or 443 if those are chosen in Settings.
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable --quiet rdr
systemctl restart rdr

echo "== Waiting for RDR to answer on port $PORT"
UP=0
for _ in $(seq 1 60); do
    if python3 -c 'import sys, urllib.request; urllib.request.urlopen("http://%s:%s/" % (sys.argv[1], sys.argv[2]), timeout=2).read(1)' "$HOST" "$PORT" >/dev/null 2>&1; then
        UP=1
        break
    fi
    sleep 1
done
if [ "$UP" != 1 ]; then
    journalctl -u rdr -n 40 --no-pager >&2 || true
    die "RDR did not start. The service log is above; see it again with: journalctl -u rdr"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "== RDR $VERSION is running"
echo "   Open http://${IP:-<this-machine>}:$PORT and go to Admin to set the admin password."
echo "   Service: systemctl status rdr     Log: journalctl -u rdr -f"
echo "   App: $APP_DIR     Data: $DATA_DIR"
echo "   Server time is $(date '+%Y-%m-%d %H:%M %Z'). RDR records times with this clock."
echo "   Wrong zone? timedatectl set-timezone <Area/City> && systemctl restart rdr"
