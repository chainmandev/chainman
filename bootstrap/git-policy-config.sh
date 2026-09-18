#!/bin/sh
# Copy only supported external policy, retaining include order and conditions.
# Each NUL-delimited record is passed as one literal argument by xargs.
set -eu
mode=$1
source=$2
output=$3
destination=$4
depth=$5
self=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)/git-policy-config.sh
allowed='^(include(if\..*)?\.path|core\.(autocrlf|eol|hookspath|attributesfile)|user\.(name|email|signingkey)|commit\.gpgsign|gpg\.(format|program|openpgp\.program|ssh\.(program|defaultkeycommand)|x509\.program))$'
if [ "$mode" = file ]; then
    case "$source" in /*) ;; *) source=$PWD/$source ;; esac
    [ -e "$source" ] || exit 0
    [ "$depth" -le 10 ] || {
        echo 'chainman: external Git policy includes exceed depth 10.' >&2
        exit 2
    }
    records=$(mktemp "$output/records.XXXXXXXX")
    status=0
    git config --file "$source" --no-includes --null --get-regexp "$allowed" > "$records" || status=$?
    case "$status" in 0 | 1) ;; *) exit "$status" ;; esac
    if [ -s "$records" ]; then
        xargs -0 -n 1 sh "$self" entry "$source" "$output" "$destination" "$depth" < "$records"
    fi
    rm -- "$records"
    exit 0
fi
entry=$6
newline='
'
key=${entry%%"$newline"*}
if [ "$key" = "$entry" ]; then
    case "$key" in core.autocrlf | commit.gpgsign) value=true ;; *) value= ;; esac
else value=${entry#*"$newline"}; fi
expand_path() {
    git -c "chainman.path=$1" config --path --get chainman.path
}
case "$key" in
    include.path | includeif.*.path)
        case "$key" in
            include.path | includeif.gitdir:*.path | includeif.gitdir/i:*.path | includeif.onbranch:*.path) ;;
            *)
                echo 'chainman: container Git policy supports gitdir and onbranch conditions; use CHAINMAN_MODE=host-nix for other conditions, including remote-dependent hasconfig includes.' >&2
                exit 2
                ;;
        esac
        [ -n "$value" ] || {
            echo 'chainman: empty Git policy include.' >&2
            exit 2
        }
        included=$(expand_path "$value")
        case "$included" in /*) ;; *) included=$(dirname -- "$source")/$included ;; esac
        [ -e "$included" ] || exit 0
        child=$(mktemp "$output/config.XXXXXXXX")
        sh "$self" file "$included" "$output" "$child" "$((depth + 1))"
        case "$key" in
            includeif.gitdir:* | includeif.gitdir/i:*)
                prefix=${key%%:*}
                pattern=${key#*:}
                pattern=${pattern%.path}
                # Git expands this literal tilde using the host HOME.
                # shellcheck disable=SC2088
                case "$pattern" in
                    '~/'*) pattern=$(expand_path "$pattern") ;;
                    ./*) pattern=$(dirname -- "$source")/${pattern#./} ;;
                esac
                key=$prefix:$pattern.path
                ;;
        esac
        value=/chainman-git-policy/${child##*/}
        ;;
    core.hookspath) value=$(expand_path "$value") ;;
    core.attributesfile)
        attributes=$(expand_path "$value")
        captured=$(mktemp "$output/attributes.XXXXXXXX")
        if [ -n "$attributes" ] && [ "$attributes" != /dev/null ] && [ -e "$attributes" ]; then
            [ -f "$attributes" ] && [ -r "$attributes" ] || {
                echo 'chainman: external Git attributes must be a readable regular file.' >&2
                exit 2
            }
            cat -- "$attributes" > "$captured"
        fi
        value=/chainman-git-policy/${captured##*/}
        ;;
esac
git config --file "$destination" --add "$key" "$value"
