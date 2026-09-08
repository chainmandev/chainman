#!/bin/sh
# Copy this launcher and fetch.nix (as chainman-fetch.nix) into consumer scripts/.
# The pinned image and this companion are the bootstrap trust base.
# Child shell programs expand their own positional and environment values.
# shellcheck disable=SC2016
set -eu
fail() {
    printf 'Chainman bootstrap: %s\n' "$*" >&2
    exit 2
}
single_line() {
    case "$1" in *'
'* | *''*) fail 'Newlines are not supported in bootstrap paths or options.' ;; esac
}
script_dir=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
self=$script_dir/$(basename -- "$0")
root=$(CDPATH='' cd -P -- "${CHAINMAN_PROJECT_ROOT:-$script_dir/..}" && pwd)
single_line "$root"
helper=$script_dir/chainman-fetch.nix
[ -f "$helper" ] || helper=$script_dir/fetch.nix
[ -f "$helper" ] && [ ! -L "$helper" ] || fail 'Missing regular chainman-fetch.nix companion.'
expression='import (builtins.toPath (builtins.getEnv "CHAINMAN_BOOTSTRAP_HELPER")) {
    root = builtins.getEnv "CHAINMAN_PROJECT_ROOT";
    archive = builtins.getEnv "CHAINMAN_ARCHIVE";
    action = builtins.getEnv "CHAINMAN_BOOTSTRAP_ACTION";
}'
mode=${CHAINMAN_MODE:-container-nix}
case "$mode" in host-nix | container-nix) ;; *) fail 'CHAINMAN_MODE must be host-nix or container-nix.' ;; esac
if [ "$mode" = container-nix ]; then
    case "$root$script_dir" in *,*) fail 'Container mount paths cannot contain commas.' ;; esac
    case "$root" in / | "${HOME:-/}") fail 'Consumer root cannot be the host root or home directory.' ;; esac
    case "${HOME:-/}/" in "$script_dir/"*) fail 'The launcher cannot require a blanket home mount.' ;; esac
fi
if [ -n "${CHAINMAN_ACTIVE_MODE:-}" ] && [ "$CHAINMAN_ACTIVE_MODE" != "$mode" ]; then
    fail 'Start a different mode outside the active development shell.'
fi
cd "$root"
[ -f chainman.lock ] && [ ! -L chainman.lock ] || fail 'Missing regular chainman.lock.'
[ ! -L "$root/.chainman" ] || fail '.chainman must be a real directory.'

if [ "$mode" = host-nix ] || [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" = 1 ]; then
    # Nix assigns TMPDIR on every shell entry. Keep the caller's selected base
    # across the bootstrap and later profile refreshes, within this mode only.
    if [ -n "${TMPDIR:-}" ]; then
        export CHAINMAN_TEMP_BASE="$TMPDIR"
    fi
    nix_bin=${CHAINMAN_NIX_BIN:-nix}
    if [ -n "${CHAINMAN_NIX_BIN:-}" ]; then
        case "$nix_bin" in /*) ;; *) fail 'CHAINMAN_NIX_BIN must be an absolute executable path.' ;; esac
        [ -x "$nix_bin" ] || fail 'CHAINMAN_NIX_BIN is not executable.'
    else
        command -v nix > /dev/null 2>&1 || fail 'Host mode requires Nix; no host-language fallback is used.'
    fi
    nix_eval() {
        CHAINMAN_BOOTSTRAP_HELPER=$helper CHAINMAN_PROJECT_ROOT=$root CHAINMAN_BOOTSTRAP_ACTION=$1 \
            "$nix_bin" --extra-experimental-features 'nix-command flakes' eval --impure --raw --expr "$expression"
    }
    metadata=$(nix_eval metadata)
    {
        IFS= read -r content_id
        IFS= read -r nar_hash
        IFS= read -r archive
    } << EOF
$metadata
EOF
    if [ "$archive" != - ]; then
        case "$archive" in /*) ;; *) archive=$root/$archive ;; esac
        single_line "$archive"
        # Every component must be real; a local override cannot escape via links.
        probe=$archive
        while [ "$probe" != / ]; do
            [ ! -L "$probe" ] || fail 'Archive paths must not contain symlinks.'
            probe=$(dirname -- "$probe")
        done
        [ -f "$archive" ] || fail 'Local archive is missing or is not a regular file.'
        CHAINMAN_ARCHIVE=$archive
        export CHAINMAN_ARCHIVE
    fi
    store=$(nix_eval fetch)
    # Archives are source distributions: symlinks are excluded before evaluating
    # even the verified flake, so extraction cannot introduce an outside path.
    [ -z "$(find "$store" -type l -print -quit)" ] || fail 'Runtime archives must not contain symlinks.'
    export CHAINMAN_MODE="$mode" CHAINMAN_ACTIVE_MODE="$mode"
    # Core entry replaces an external project shell. Its old profile token no
    # longer describes PATH, even when the project inputs themselves are unchanged.
    unset IN_NIX_SHELL CHAINMAN_ACTIVE_PROFILE CHAINMAN_ACTIVE_FINGERPRINT
    exec "$nix_bin" --extra-experimental-features 'nix-command flakes' develop "path:$store/nix#core" --no-write-lock-file \
        --command python3 -c '
import fcntl, os, pathlib, shutil, stat, subprocess, sys, tempfile, urllib.parse
root, content_id, expected, store, *args = sys.argv[1:]
nix = os.path.join(os.environ["CHAINMAN_RUNTIME_NIX_BIN"], "nix")
cache = pathlib.Path(root) / ".chainman"
try:
    cache.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = os.open(".bootstrap.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(lock).st_mode):
        raise ValueError("bootstrap lock is not a regular file")
    fcntl.flock(lock, fcntl.LOCK_EX)
    def verify_directory():
        a, b = os.stat(cache, follow_symlinks=False), os.fstat(directory)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise ValueError("runtime directory changed during bootstrap")
    verify_directory()
    # Relative filesystem operations stay attached to the open real directory,
    # even if another process renames its former pathname during installation.
    os.fchdir(directory)
    runtime = pathlib.Path(content_id)
    def verify(path):
        if path.is_symlink() or not path.is_dir():
            raise ValueError("runtime must be a real directory")
        actual = subprocess.check_output([nix, "--extra-experimental-features", "nix-command", "hash", "path", str(path)], text=True).strip()
        if actual != expected:
            raise ValueError("installed runtime failed NAR verification")
    if not os.path.lexists(runtime):
        stage = pathlib.Path(tempfile.mkdtemp(prefix=".install-", dir="."))
        try:
            shutil.copytree(store, stage, dirs_exist_ok=True, symlinks=True)
            verify(stage)
            os.rename(stage, runtime)
        finally:
            if stage.exists():
                for current, dirs, files in os.walk(stage):
                    os.chmod(current, 0o700)
                shutil.rmtree(stage)
    verify(runtime)
    verify_directory()
    runtime = cache / content_id
    os.close(lock)
    os.close(directory)
    os.chdir(root)
    os.environ.update(CHAINMAN_RUNTIME=str(runtime), CHAINMAN_ROOT=root, CHAINMAN_PROJECT_ROOT=root,
                      PYTHONDONTWRITEBYTECODE="1")
    os.execv(nix, [nix, "--extra-experimental-features", "nix-command flakes", "develop",
        "path:" + urllib.parse.quote(str(runtime / "nix"), safe="/") + "#core", "--no-write-lock-file", "--command",
        "python3", str(runtime / "scripts/chainman.py"), "--root", root, *args])
except (OSError, ValueError, subprocess.CalledProcessError) as error:
    sys.exit("Chainman bootstrap: " + str(error))
' "$root" "$content_id" "$nar_hash" "$store" "$@"
fi

engine=${CHAINMAN_CONTAINER_ENGINE:-${CHAINMAN_ENGINE:-}}
if [ -z "$engine" ]; then
    for candidate in docker podman; do
        if command -v "$candidate" > /dev/null 2>&1; then
            engine=$candidate
            break
        fi
    done
fi
case "$engine" in docker | podman) ;; *) fail 'Container mode requires Docker or Podman.' ;; esac
image=docker.io/nixos/nix:2.33.3@sha256:c2f7db70a432d00c6759af108ff4fbc74a4c00e2d4517162e72338e7b9449c1f
uid=$(id -u)
gid=$(id -g)
container_uid=$uid
container_gid=$gid
if [ "$engine" = docker ]; then
    security_options=$("$engine" info --format '{{range .SecurityOptions}}{{println .}}{{end}}') || fail 'Cannot determine Docker daemon identity mapping.'
    while IFS= read -r option; do
        if [ "$option" = name=rootless ]; then
            container_uid=0
            container_gid=0
        fi
    done << EOF
$security_options
EOF
fi
volume=chainman-nix-$uid
temporary=$(mktemp -d "${TMPDIR:-/tmp}/chainman-bootstrap.XXXXXXXX")
trap 'rm -rf -- "$temporary"' EXIT HUP INT TERM
# Select an explicit architecture before initializing or evaluating in the image.
# Its Nix store volume must not inherit the default architecture's profile links.
platform=
if [ -n "${CHAINMAN_CONTAINER_OPTIONS_FILE:-}" ]; then
    options_file=$CHAINMAN_CONTAINER_OPTIONS_FILE
    case "$options_file" in /*) ;; *) options_file=$root/$options_file ;; esac
    [ -f "$options_file" ] && [ ! -L "$options_file" ] || fail 'Container options file must be a regular file.'
    {
        cat "$options_file"
        printf '\n'
    } > "$temporary/extra"
    options_file=$temporary/extra
    while IFS= read -r option || [ -n "$option" ]; do
        [ -n "$option" ] || continue
        IFS= read -r value || [ -n "$value" ] || fail 'Container option lacks a value.'
        if [ "$option" = --platform ]; then
            case "$value" in linux/amd64 | linux/arm64) ;; *) fail 'Container platform must be linux/amd64 or linux/arm64.' ;; esac
            [ -z "$platform" ] || fail 'Container platform must be specified once.'
            platform=$value
        fi
    done < "$options_file"
fi
if [ -n "$platform" ]; then volume=$volume-${platform#linux/}; fi
downloads_volume=${volume}-downloads
run() {
    if [ -n "$platform" ]; then set -- --platform "$platform" "$@"; fi
    if [ "$engine" = podman ]; then "$engine" run --userns=keep-id "$@"; else "$engine" run "$@"; fi
}
# Capability-free UID 0 still owns /. Keep Nix's nonexistent build HOME from
# being created accidentally, without changing writable mounts or disk-backed /tmp.
container_init='
    if [ "$(id -u)" = 0 ]; then chmod 0555 /; fi
    mkdir -p "$HOME"
    exec "$@"
'
# Only the named Nix store is prepared as root. No host directory is mounted here.
run --rm --user 0:0 --mount "type=volume,src=$volume,dst=/nix" --mount "type=volume,src=$downloads_volume,dst=/chainman-downloads" "$image" sh -eu -c '
    mkdir -p /nix/store /nix/var
    chown "$1:$2" /nix /nix/store
    [ ! -d /nix/store/.links ] || chown "$1:$2" /nix/store/.links
    chown -R "$1:$2" /nix/var
    chown "$1:$2" /chainman-downloads
' sh "$container_uid" "$container_gid"
run --rm --user "$container_uid:$container_gid" --security-opt no-new-privileges --cap-drop ALL \
    --mount "type=volume,src=$volume,dst=/nix" --mount "type=bind,src=$root,dst=$root,readonly" \
    --mount "type=bind,src=$script_dir,dst=/chainman-bootstrap,readonly" --env HOME=/tmp/chainman-home \
    --env 'NIX_CONFIG=build-users-group =' \
    --env "CHAINMAN_BOOTSTRAP_HELPER=/chainman-bootstrap/$(basename -- "$helper")" \
    --env "CHAINMAN_PROJECT_ROOT=$root" --env CHAINMAN_BOOTSTRAP_ACTION=options \
    "$image" sh -eu -c "$container_init" \
    sh nix --extra-experimental-features 'nix-command flakes' eval --impure --raw --expr "$expression" > "$temporary/options"

# A selected name is passed to the engine without its value in the argument list.
env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' > "$temporary/names"
printf '%s' "${CHAINMAN_FORWARD_ENV:-}" | tr ',' '\n' > "$temporary/patterns"
printf '\n' >> "$temporary/patterns"
if [ -n "${CHAINMAN_CONTAINER_OPTIONS_FILE:-}" ]; then
    # Keep the public options-file allowlist separate from helper-only records.
    while IFS= read -r option; do
        [ -n "$option" ] || continue
        case "$option" in --publish | -p | --mount | -v | --volume | --add-host | --hostname | --label | --name | --network | --platform) ;; *) fail "Unsupported container option: $option" ;; esac
        IFS= read -r value || fail 'Container option lacks a value.'
        printf '%s\n%s\n' "$option" "$value" >> "$temporary/options"
    done < "$temporary/extra"
fi
set -- "$image" sh -eu -c "$container_init" sh "$self" "$@"
while IFS= read -r option; do
    [ -n "$option" ] || continue
    IFS= read -r value || fail 'Container option lacks a value.'
    single_line "$value"
    [ -n "$value" ] || fail 'Empty container option value.'
    case "$option" in
        --env-pattern)
            printf '%s\n' "$value" >> "$temporary/patterns"
            continue
            ;;
        --mount)
            case "$value" in type=bind,src=*,dst=*) ;; *) fail 'Only explicit bind mounts are accepted.' ;; esac
            source=${value#type=bind,src=}
            source=${source%%,dst=*}
            target=${value#*,dst=}
            target=${target%%,*}
            suffix=${value#*,dst=}
            suffix=${suffix#"$target"}
            case "$suffix" in '' | ,readonly | ,ro) ;; *) fail 'Unsupported mount option.' ;; esac
            ;;
        -v | --volume)
            source=${value%%:*}
            remainder=${value#*:}
            target=${remainder%%:*}
            case "$remainder" in "$target" | "$target:ro" | "$target:rw") ;; *) fail 'Unsupported volume option.' ;; esac
            ;;
        --network)
            case "$value" in host | bridge) ;; *) fail 'Container network must be host or bridge.' ;; esac
            set -- "$option" "$value" "$@"
            continue
            ;;
        --platform)
            case "$value" in linux/amd64 | linux/arm64) ;; *) fail 'Container platform must be linux/amd64 or linux/arm64.' ;; esac
            set -- "$option" "$value" "$@"
            continue
            ;;
        --publish | -p | --add-host | --hostname | --label | --name)
            set -- "$option" "$value" "$@"
            continue
            ;;
        *) fail "Unsupported container option: $option" ;;
    esac
    case "$source" in /*) ;; *) source=$root/$source ;; esac
    case "$source$target" in *,*) fail 'Container mount paths cannot contain commas.' ;; esac
    [ -e "$source" ] && [ ! -S "$source" ] || fail 'Mount source must exist and cannot be a socket.'
    if [ -d "$source" ]; then
        source=$(CDPATH='' cd -P -- "$source" && pwd)
    else source=$(CDPATH='' cd -P -- "$(dirname -- "$source")" && pwd)/$(basename -- "$source"); fi
    case "$source" in / | "${HOME:-/}" | /run | /var/run | */docker.sock | */podman.sock) fail 'Blanket host or socket mounts are not supported.' ;; esac
    case "${HOME:-/}/" in "$source/"*) fail 'Blanket host or socket mounts are not supported.' ;; esac
    case "$target" in /*) ;; *) fail 'Mount target must be absolute.' ;; esac
    case "$target/" in *'/../'* | *'/./'* | *'//'*) fail 'Mount target must be normalized.' ;; esac
    case "$target" in / | /tmp | /nix | /nix/* | /chainman-bootstrap | /chainman-downloads | /chainman-downloads/* | "$root" | "$root/.chainman" | "$root/.chainman/"*) fail 'Mount shadows a bootstrap directory.' ;; esac
    case "$root/" in "$target/"*) fail 'Mount shadows the project through an ancestor.' ;; esac
    if [ "$target" = /tmp/chainman-home ]; then
        case "$source" in "$root"/*) ;; *) fail 'Persistent container HOME must be a project-contained directory.' ;; esac
        [ -d "$source" ] || fail 'Persistent container HOME must be a directory.'
    fi
    # Normalize volume forms so source paths are interpreted on the host.
    readonly=
    case "$value" in *,readonly | *,ro | *:ro) readonly=,readonly ;; esac
    set -- --mount "type=bind,src=$source,dst=$target$readonly" "$@"
done < "$temporary/options"
while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    case "$pattern" in [!A-Za-z_]* | *[!A-Za-z0-9_\*\?]*) fail 'Invalid forwarded environment pattern.' ;; esac
    while IFS= read -r name; do
        # These validated values intentionally select names with shell globs.
        # shellcheck disable=SC2254
        case "$name" in $pattern)
            case "$name" in CHAINMAN_* | TOOLCHAIN_CONTAINER | HOME | PATH | GIT_CONFIG_* | NIX_*) continue ;; esac
            set -- --env "$name" "$@"
            ;;
        esac
    done < "$temporary/names"
done < "$temporary/patterns"

# A linked worktree needs only its Git administrative directory, not its other
# checkout. Identity and signing policy cross the boundary as effective settings.
count=0
policy_unavailable=0
if command -v git > /dev/null 2>&1; then
    git_owner=$(git -C "$root" rev-parse --show-toplevel 2> /dev/null) || git_owner=
    if [ -n "$git_owner" ]; then
        git_owner=$(CDPATH='' cd -- "$git_owner" && pwd -P)
    fi
    if [ "$git_owner" = "$root" ]; then
        admin=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir)
        case "$admin" in "$root"/*) ;; *)
            single_line "$admin"
            case "$admin" in / | "${HOME:-/}" | *,*) fail 'Unsafe Git administrative mount.' ;; esac
            set -- --mount "type=bind,src=$admin,dst=$admin" "$@"
            ;;
        esac
        gitdir=$(git -C "$root" rev-parse --absolute-git-dir)
        case "$gitdir" in "$root"/* | "$admin" | "$admin"/*) ;; *)
            single_line "$gitdir"
            case "$gitdir" in / | "${HOME:-/}" | *,*) fail 'Unsafe Git administrative mount.' ;; esac
            set -- --mount "type=bind,src=$gitdir,dst=$gitdir" "$@"
            ;;
        esac
    fi
    for key in user.name user.email user.signingkey commit.gpgsign gpg.format gpg.program gpg.openpgp.program gpg.ssh.program gpg.ssh.defaultKeyCommand gpg.x509.program; do
        status=0
        if [ "$git_owner" = "$root" ]; then
            value=$(git -C "$root" config --get "$key" 2> /dev/null) || status=$?
        else
            # Preserve system/global policy without borrowing enclosing repo config.
            value=$(git -C "$root" --git-dir=/dev/null config --get "$key" 2> /dev/null) || status=$?
        fi
        case "$status" in
            0)
                set -- --env "GIT_CONFIG_KEY_$count=$key" --env "GIT_CONFIG_VALUE_$count=$value" "$@"
                count=$((count + 1))
                ;;
            1) ;;
            *) policy_unavailable=1 ;;
        esac
    done
elif [ -f "${GIT_CONFIG_GLOBAL:-${HOME:-/}/.gitconfig}" ]; then
    policy_unavailable=1
fi
if [ -n "${CHAINMAN_ARCHIVE:-}" ]; then
    archive=$CHAINMAN_ARCHIVE
    case "$archive" in /*) ;; *) archive=$root/$archive ;; esac
    single_line "$archive"
    case "$archive" in "$root"/*) ;; *)
        [ -f "$archive" ] && [ ! -L "$archive" ] || fail 'Local archive override must be a regular file.'
        set -- --mount "type=bind,src=$archive,dst=$archive,readonly" "$@"
        ;;
    esac
    set -- --env "CHAINMAN_ARCHIVE=$archive" "$@"
fi
case "$self" in "$root"/*) ;; *) set -- --mount "type=bind,src=$script_dir,dst=$script_dir,readonly" "$@" ;; esac
set -- --rm --init --interactive --user "$container_uid:$container_gid" --security-opt no-new-privileges --cap-drop ALL \
    --mount "type=volume,src=$volume,dst=/nix" --mount "type=bind,src=$root,dst=$root" \
    --mount "type=volume,src=$downloads_volume,dst=/chainman-downloads" --env TOOLCHAIN_DOWNLOAD_CACHE=/chainman-downloads \
    --workdir "$root" \
    --env HOME=/tmp/chainman-home --env CHAINMAN_MODE=container-nix --env CHAINMAN_BOOTSTRAP_CONTAINER=1 \
    --env 'NIX_CONFIG=build-users-group =' \
    --env "CHAINMAN_PROJECT_ROOT=$root" --env TOOLCHAIN_CONTAINER=1 --env "GIT_CONFIG_COUNT=$count" \
    --env "TOOLCHAIN_GIT_POLICY_UNAVAILABLE=$policy_unavailable" --env CI --env TERM \
    --env GIT_AUTHOR_NAME --env GIT_AUTHOR_EMAIL --env GIT_COMMITTER_NAME --env GIT_COMMITTER_EMAIL "$@"
if [ -t 0 ] && [ -t 1 ]; then set -- --tty "$@"; fi
rm -rf -- "$temporary"
trap - EXIT HUP INT TERM
if [ "$engine" = podman ]; then exec "$engine" run --userns=keep-id "$@"; fi
exec "$engine" run "$@"
