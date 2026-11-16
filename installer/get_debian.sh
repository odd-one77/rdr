#!/usr/bin/env bash
# Download an RDR release from GitHub and install or upgrade it on Debian 12 or newer.
#
# Run as root in the container:
#     bash get_debian.sh            # the latest release
#     bash get_debian.sh 0.9.8      # one named release
# Running it again upgrades the app and never touches the data in /var/lib/rdr.
#
# A public repo needs nothing else. For a private repo set GH_TOKEN to a GitHub
# fine-grained token with "Contents: read" on it (or type it when asked).
#
# RDR_DOWNLOAD_ONLY=1 saves rdr-debian-<version>.tar.gz in the current folder
# and installs nothing. RDR_REPO picks another repo (owner/name).
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"

die() { echo "!! $*" >&2; exit 1; }

# No token: the public download links. Sets TAG and NAME and saves the bundle, or returns 1.
fetch_public() {
    local repo="$1" want="$2" page
    if [ "$want" = latest ]; then
        page="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$repo/releases/latest" < /dev/null)" || return 1
        case "$page" in */releases/tag/v*) TAG="${page##*/}" ;; *) return 1 ;; esac
    else
        TAG="v$want"
    fi
    NAME="rdr-debian-${TAG#v}.tar.gz"
    echo "== Downloading $NAME ($TAG)"
    curl -fsSL --retry 3 -o "$WORK/$NAME" "https://github.com/$repo/releases/download/$TAG/$NAME" < /dev/null
}

# With a token: the GitHub API, which also reads a private repo.
fetch_with_token() {
    local repo="$1" want="$2" token="$3"
    local api="https://api.github.com/repos/$repo/releases" url code found asset
    # The token goes to curl in a file, so it never shows in the process list.
    ( umask 077; printf 'Authorization: Bearer %s\n' "$token" > "$WORK/auth" )
    url="$api/latest"
    [ "$want" = latest ] || url="$api/tags/v$want"
    code="$(curl -sS -o "$WORK/release.json" -w '%{http_code}' -H @"$WORK/auth" \
        -H 'Accept: application/vnd.github+json' "$url" < /dev/null)" || die "Could not reach api.github.com."
    case "$code" in
        200) ;;
        401) die "GitHub did not accept the token. Check it has not expired." ;;
        403|404) die "Release not found ($code). Check the token can read $repo and that the version exists." ;;
        *) die "GitHub answered $code." ;;
    esac
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
    read -r TAG asset NAME <<< "$found"
    echo "== Downloading $NAME ($TAG)"
    curl -fsSL --retry 3 -o "$WORK/$NAME" -H @"$WORK/auth" -H 'Accept: application/octet-stream' "$asset" < /dev/null \
        || die "The download failed."
}

# Everything runs from main so that "curl ... | bash" has read the whole script first.
main() {
    local repo="${RDR_REPO:-odd-one77/rdr}"
    local want="${1:-latest}"
    want="${want#v}"
    local token="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    local only="${RDR_DOWNLOAD_ONLY:-0}"

    [ "$only" = 1 ] || [ "$(id -u)" = 0 ] || die "Run this as root (or with sudo)."

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
    TAG=""
    NAME=""

    echo "== Looking up the $want release of $repo"
    if [ -n "$token" ]; then
        fetch_with_token "$repo" "$want" "$token"
    elif ! fetch_public "$repo" "$want" 2>/dev/null; then
        # Not public, or no such release. A token can still read a private repo.
        if { : > /dev/tty; } 2>/dev/null; then
            echo "   Nothing public found. A private repo needs a token."
            printf 'GitHub token with read access to %s: ' "$repo" > /dev/tty
            read -rs token < /dev/tty || true
            printf '\n' > /dev/tty
        fi
        [ -n "$token" ] || die "No public release found. Check the version, or set GH_TOKEN if $repo is private."
        fetch_with_token "$repo" "$want" "$token"
    fi

    tar -tzf "$WORK/$NAME" > "$WORK/list" 2>/dev/null || die "The download is not an RDR Debian bundle."
    grep -qx "${NAME%.tar.gz}/installer/install_debian.sh" "$WORK/list" \
        || die "The download is not an RDR Debian bundle."

    if [ "$only" = 1 ]; then
        mv "$WORK/$NAME" "./$NAME"
        echo "== Saved $NAME in $(pwd)"
        return 0
    fi

    tar -xzf "$WORK/$NAME" -C "$WORK"
    bash "$WORK/${NAME%.tar.gz}/installer/install_debian.sh" < /dev/null
}

main "$@"
