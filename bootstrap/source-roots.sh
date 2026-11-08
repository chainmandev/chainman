# Verified declaration data enters here before any external source mount exists.
# The host needs only POSIX shell tools; no host language runtime resolves paths.
canonical_source_inventory() {
    while IFS= read -r source_key; do
        IFS= read -r source_value || fail 'External source declaration lacks a path.'
        case "$source_key" in '' | [!A-Za-z_]* | *[!A-Za-z0-9_]* | CHAINMAN_* | TOOLCHAIN_*) fail 'Invalid external source variable.' ;; esac
        single_line "$source_value"
        case "$source_value" in /*) ;; *) fail 'External source root requires an absolute directory.' ;; esac
        case "$source_value" in *,*) fail 'External source paths cannot contain commas.' ;; esac
        [ -d "$source_value" ] || fail "External source root is not a directory: $source_key"
        source_value=$(CDPATH='' cd -P -- "$source_value" && pwd) || fail 'External source root cannot be resolved.'
        single_line "$source_value"
        case "$source_value" in / | "${HOME:-/}" | /run | /var/run | /nix | /nix/* | /tmp | /tmp/chainman-home | /chainman-*) fail 'External source root must be a narrow source directory.' ;; esac
        case "$root/" in "$source_value/"*) fail 'External source root cannot contain the consumer.' ;; esac
        case "${HOME:-/}/" in "$source_value/"*) fail 'External source root cannot contain the host home.' ;; esac
        case "$source_value" in *,*) fail 'External source paths cannot contain commas.' ;; esac
        printf '%s\n%s\n' "$source_key" "$source_value"
    done
}
