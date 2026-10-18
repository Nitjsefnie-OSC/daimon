#!/bin/sh
# Select a GitHub App placeholder by repository. The placeholder is not a
# secret: egress substitutes it only for allowed GitHub hosts. Keep it out of
# ordinary output so commands and logs do not expose even placeholder values.
set -eu

denied() {
    printf '%s is not granted to this agent\n' "$1" >&2
    exit 2
}

usage() {
    printf 'usage: sh github-repo.sh owner/repo <gh subcommand...>\n' >&2
    exit 2
}

# Sets selected_repo and variable for an exact, normalized map entry.
lookup() {
    requested=$1
    key=$(printf '%s' "$requested" | tr '[:upper:]' '[:lower:]')
    key=${key%.git}
    case "$key" in
        */*) ;;
        *) return 1 ;;
    esac
    case "$key" in
        */*/*|/*|*/|*[!a-z0-9._/-]*) return 1 ;;
    esac

    map_file=$(dirname "$0")/github-repos.tsv
    [ -r "$map_file" ] || return 1
    tab=$(printf '\t')
    selected_repo=
    variable=
    while IFS="$tab" read -r mapped_repo mapped_variable; do
        mapped_key=$(printf '%s' "$mapped_repo" | tr '[:upper:]' '[:lower:]')
        if [ "$mapped_key" = "$key" ]; then
            selected_repo=$mapped_repo
            variable=$mapped_variable
            break
        fi
    done < "$map_file"
    case "$variable" in
        GH_TOKEN_I*_*) ;;
        *) return 1 ;;
    esac
    suffix=${variable#GH_TOKEN_I}
    installation=${suffix%%_*}
    chunk=${suffix#*_}
    case "$installation" in
        ''|*[!0-9]*) return 1 ;;
    esac
    case "$chunk" in
        ''|*[!0-9]*) return 1 ;;
    esac
    return 0
}

if [ "${1:-}" = --git-credential ]; then
    [ "${2:-}" = get ] || exit 0
    protocol=
    host=
    path=
    while IFS= read -r field && [ -n "$field" ]; do
        case "$field" in
            protocol=*) [ -z "$protocol" ] || exit 0; protocol=${field#protocol=} ;;
            host=*) [ -z "$host" ] || exit 0; host=${field#host=} ;;
            path=*) [ -z "$path" ] || exit 0; path=${field#path=} ;;
        esac
    done
    [ "$protocol" = https ] && [ "$host" = github.com ] || exit 0
    lookup "$path" || exit 0
    origin_url=$(git config --get remote.origin.url 2>/dev/null || true)
    if [ -n "$origin_url" ]; then
        case "$origin_url" in
            https://github.com/*) origin_repo=${origin_url#https://github.com/} ;;
            *) exit 0 ;;
        esac
        origin_key=$(printf '%s' "$origin_repo" | tr '[:upper:]' '[:lower:]')
        origin_key=${origin_key%.git}
        [ "$origin_key" = "$key" ] || exit 0
    fi
    placeholder=$(printenv "$variable") || exit 0
    [ -n "$placeholder" ] || exit 0
    printf 'username=x-access-token\npassword=%s\n' "$placeholder"
    exit 0
fi

if [ "$#" -lt 2 ]; then
    usage
fi

requested=$1
shift
case "$1" in
    repo|pr|issue|api|release|run|workflow|label|search|browse|clone) ;;
    auth|config|alias|extension|secret|variable)
        printf 'gh %s is unavailable through this helper\n' "$1" >&2
        exit 2
        ;;
    *) usage ;;
esac
lookup "$requested" || denied "$requested"
placeholder=$(printenv "$variable") || denied "$requested"
[ -n "$placeholder" ] || denied "$requested"

if [ "$1" = clone ]; then
    shift
    if [ "$#" -gt 1 ]; then
        printf 'usage: sh github-repo.sh owner/repo clone [dir]\n' >&2
        exit 2
    fi
    clone_dir=${selected_repo##*/}
    if [ "$#" -eq 1 ]; then
        clone_dir=$1
        GH_TOKEN="$placeholder" gh repo clone "$selected_repo" "$clone_dir"
    else
        GH_TOKEN="$placeholder" gh repo clone "$selected_repo"
    fi
    git -C "$clone_dir" config --add credential.https://github.com.helper ''
    git -C "$clone_dir" config --add credential.https://github.com.helper \
        '!sh /mnt/session/uploads/github-repo.sh --git-credential'
    git -C "$clone_dir" config credential.useHttpPath true
else
    GH_TOKEN="$placeholder" gh "$@"
fi
