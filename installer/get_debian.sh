#!/usr/bin/env bash
# Download an RDR release from GitHub and install or upgrade it on Debian 12 or newer.
#
# Run as root in the container. The repo is private, so it needs a GitHub
# fine-grained token with "Contents: read" on the repo:
#     export GH_TOKEN=github_pat_...
#     bash get_debian.sh            # the latest release
#     bash get_debian.sh 0.9.7      # one named release
# Without GH_TOKEN it asks for the token. Running it again upgrades the app
# and never touches the data in /var/lib/rdr.
#
# RDR_DOWNLOAD_ONLY=1 saves rdr-debian-<version>.tar.gz in the current folder
# and installs nothing. RDR_REPO picks another repo (owner/name).
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"

die() { echo "!! $*" >&2; exit 1; }

# Everything runs from main so that "curl ... | bash" has read the whole script first.
main() {
    local repo="${RDR_REPO:-odd-one77/rdr}"
    local want="${1:-latest}"
    want="${want#v}"
    local token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    local only="${RDR_DOWNLOAD_ONLY:-0}"
    local api="https://api.github.com/repos/$repo/releases"

    [ "$only" = 1 ] || [ "$(id -u)" = 0 ] || die "Run this as root (or with sudo)."

    if [ -z "$token" ] && { : > /dev/tty; } 2>/dev/null; then
        printf 'GitHub token with read access to %s: ' "$repo" > /dev/tty
        read -rs token < /dev/tty || true
        printf '\n' > /dev/tty
    fi

    local missing=""
    command -v curl >/dev/null 2>&1 || missing="$missing curl"
    command -v python3 >/dev/null 2>&1 || missing="$missing python3"
    [ -e /etc/ssl/certs/ca-certificates.crt ] || missing="$missing ca-certificates"
    if [ -n "$missing" ]; then
        [ "$(id -u)" = 0 ] || die "Install these first:$missing"
        echo "== Packages:$missing"
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -q < /dev/null
        # shellcheck disable=SC2086
        apt-get install -y -q --no-install-recommends $missing < /dev/null
    fi

    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    # The token goes to curl in a file, so it never shows in the process list.
    ( umask 077; : > "$WORK/auth"; [ -z "$token" ] || printf 'Authorization: Bearer %s\n' "$token" > "$WORK/auth" )

    local url="$api/latest"
    [ "$want" = latest ] || url="$api/tags/v$want"
    echo "== Looking up the $want release of $repo"
    local code
    code="$(curl -sS -o "$WORK/release.json" -w '%{http_code}' -H @"$WORK/auth" \
        -H 'Accept: application/vnd.github+json' "$url" < /dev/null)" || die "Could not reach api.github.com."
    case "$code" in
        200) ;;
        401) die "GitHub did not accept the token. Check it has not expired." ;;
        403|404)
            if [ -z "$token" ]; then
                die "The repo is private. Set GH_TOKEN to a GitHub token with Contents: read on $repo."
            fi
            die "Release not found ($code). Check the token can read $repo and that the version exists." ;;
        *) die "GitHub answered $code." ;;
    esac

    local found
    found="$(python3 - "$WORK/release.json" <<'PY'
import json, sys
rel = json.load(open(sys.argv[1]))
for a in rel.get("assets", []):
    n = a.get("name", "")
    if n.startswith("rdr-debian-") and n.endswith(".tar.gz") and "/" not in n:
        print(rel.get("tag_name", ""), a["url"], n)
        break
PY
)"
    [ -n "$found" ] || die "That release has no Debian download. They start at version 0.9.6."
    local tag asset name
    read -r tag asset name <<< "$found"

    echo "== Downloading $name ($tag)"
    curl -fsSL --retry 3 -o "$WORK/$name" -H @"$WORK/auth" -H 'Accept: application/octet-stream' "$asset" < /dev/null \
        || die "The download failed."
    tar -tzf "$WORK/$name" > "$WORK/list" 2>/dev/null || die "The download is not an RDR Debian bundle."
    grep -qx "${name%.tar.gz}/installer/install_debian.sh" "$WORK/list" \
        || die "The download is not an RDR Debian bundle."

    if [ "$only" = 1 ]; then
        mv "$WORK/$name" "./$name"
        echo "== Saved $name in $(pwd)"
        return 0
    fi

    tar -xzf "$WORK/$name" -C "$WORK"
    bash "$WORK/${name%.tar.gz}/installer/install_debian.sh" < /dev/null
    echo "   Update and restart in Admin needs the same token once: Settings, Updates."
}

main "$@"
