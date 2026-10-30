# Privileged managed-settings replacement, run as root via `sudo /bin/sh -c <this file's text>`.
# ucode loads it from the package and passes its contents inline (see managed_files.py); it is never
# executed from disk.
#
# Usage: sh -c <script> <name> once <linux|macos> <source> <target>
#        sh -c <script> <name> session <linux|macos>
#   session mode reads `REPLACE <id> <base64 source> <base64 target>` / `QUIT` lines from stdin and
#   answers `OK <id>` or `ERROR <id> <status> <base64 stderr>`.
set -u
set -f
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
# Folders `mkdir -p` creates must stay readable to the agents whatever the caller's umask.
umask 022

mode=$1
platform=$2
shift 2

case "$platform" in
    linux|macos) ;;
    *)
        printf '%s\n' "Unsupported platform for managed-settings replacement: $platform" >&2
        exit 2
        ;;
esac

# The caller (managed_files._validate_sudo_replace_target) checks the same allowlist first so a bad
# target never prompts for a password. This check is the one that matters: this script runs as root
# and must not trust whoever writes to its arguments or stdin. Keep both lists in sync.
target_is_allowed() {
    case "$platform:$1" in
        "linux:/etc/claude-code/managed-settings.json"|\
        "linux:/etc/codex/managed_config.toml"|\
        "linux:/etc/unitygateway/managed-settings.json"|\
        "linux:/etc/claude-code/ucode-plugins/.claude-plugin/marketplace.json"|\
        "linux:/etc/claude-code/ucode-plugins/ug-headers/.claude-plugin/plugin.json"|\
        "linux:/etc/claude-code/ucode-plugins/ug-headers/hooks/hooks.json"|\
        "linux:/etc/claude-code/ucode-plugins/ug-headers/hooks/register.ts"|\
        "macos:/Library/Application Support/ClaudeCode/managed-settings.json"|\
        "macos:/etc/codex/managed_config.toml"|\
        "macos:/etc/unitygateway/managed-settings.json"|\
        "macos:/Library/Application Support/ClaudeCode/ucode-plugins/.claude-plugin/marketplace.json"|\
        "macos:/Library/Application Support/ClaudeCode/ucode-plugins/ug-headers/.claude-plugin/plugin.json"|\
        "macos:/Library/Application Support/ClaudeCode/ucode-plugins/ug-headers/hooks/hooks.json"|\
        "macos:/Library/Application Support/ClaudeCode/ucode-plugins/ug-headers/hooks/register.ts") return 0 ;;
        *) return 1 ;;
    esac
}

decode_arg() {
    if [ "$platform" = macos ]; then
        printf %s "$1" | base64 -D
    else
        printf %s "$1" | base64 -d
    fi
}

encode_arg() {
    printf %s "$1" | base64 | tr -d '\n'
}

replace_one() (
    set -eu
    source_path=$1
    target=$2
    parent=${target%/*}
    target_name=${target##*/}
    staging_template="$parent/.$target_name.ucode.XXXXXX"
    staging=
    original_flags=
    clear_flags=

    # Functions evaluated by an `if` condition do not honor errexit in every POSIX shell.
    # Check every mutating step explicitly so a failed copy can never reach the rename.
    run_step() {
        "$@" && return 0
        status=$?
        printf 'Managed-settings update failed during %s for %s\n' "$1" "$target" >&2
        exit "$status"
    }

    source_is_valid() {
        [ -f "$source_path" ] && [ ! -L "$source_path" ] || return 1
        if [ "$platform" = macos ]; then
            source_owner=$(/usr/bin/stat -f %u "$source_path" 2>/dev/null) || return 1
        else
            source_owner=$(stat -c %u "$source_path" 2>/dev/null) || return 1
        fi
        [ -n "${SUDO_UID:-}" ] || return 1
        [ "$source_owner" = "$SUDO_UID" ]
    }

    clear_staging_flags() {
        [ -n "$original_flags" ] || return 0
        if [ "$platform" = macos ]; then
            chflags "$clear_flags" "$staging"
        else
            chattr "-$original_flags" "$staging"
        fi
    }

    restore_target_flags() {
        [ -n "$original_flags" ] || return 0
        if [ "$platform" = macos ]; then
            chflags "$original_flags" "$target"
        else
            chattr "+$original_flags" "$target"
        fi
    }

    cleanup() {
        status=$?
        trap - 0 1 2 3 15
        set +e
        if [ -n "$original_flags" ] && [ -e "$target" ] && [ ! -L "$target" ]; then
            restore_target_flags
        fi
        if [ -n "$staging" ] && [ -e "$staging" ]; then
            clear_staging_flags
            rm -f "$staging"
        fi
        exit "$status"
    }
    trap cleanup 0
    trap 'exit 129' 1
    trap 'exit 130' 2
    trap 'exit 131' 3
    trap 'exit 143' 15

    if ! target_is_allowed "$target"; then
        printf '%s\n' "Refusing unexpected managed-settings target: $target" >&2
        exit 1
    fi
    if ! source_is_valid; then
        printf '%s\n' "Refusing invalid managed-settings source: $source_path" >&2
        exit 1
    fi

    if [ -L "$parent" ] || [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
        printf '%s\n' "Refusing symlinked or non-regular managed settings: $target" >&2
        exit 1
    fi

    if [ ! -d "$parent" ]; then
        run_step mkdir -p "$parent"
        run_step chown 0:0 "$parent"
        run_step chmod 0755 "$parent"
    fi
    if [ "$target" = /etc/unitygateway/managed-settings.json ]; then
        run_step chown 0:0 "$parent"
        run_step chmod 0755 "$parent"
    fi

    staging=$(mktemp "$staging_template") || exit "$?"

    if [ -L "$target" ]; then
        printf '%s\n' "Refusing to replace symlinked managed settings: $target" >&2
        exit 1
    fi

    if [ -e "$target" ]; then
        if [ "$platform" = macos ]; then
            flag_output=$(/usr/bin/stat -f %Sf "$target" 2>/dev/null || :)
            saved_ifs=$IFS
            IFS=,
            for flag in $flag_output; do
                case "$flag" in
                    schg|uchg|sappnd|uappnd)
                        if [ -n "$original_flags" ]; then
                            original_flags="$original_flags,$flag"
                            clear_flags="$clear_flags,no$flag"
                        else
                            original_flags=$flag
                            clear_flags="no$flag"
                        fi
                        ;;
                esac
            done
            IFS=$saved_ifs
            if [ -n "$original_flags" ]; then
                run_step chflags "$clear_flags" "$target"
            fi
            run_step cp -p "$target" "$staging"
        else
            attr_output=$(lsattr -d "$target" 2>/dev/null || :)
            attributes=${attr_output%% *}
            case "$attributes" in *i*) original_flags=i ;; esac
            case "$attributes" in *a*) original_flags="${original_flags}a" ;; esac
            if [ -n "$original_flags" ]; then
                run_step chattr "-$original_flags" "$target"
            fi
            run_step cp --preserve=all "$target" "$staging"
        fi
        run_step clear_staging_flags
        if ! source_is_valid; then
            printf '%s\n' "Refusing invalid managed-settings source: $source_path" >&2
            exit 1
        fi
        run_step cp "$source_path" "$staging"
    else
        if ! source_is_valid; then
            printf '%s\n' "Refusing invalid managed-settings source: $source_path" >&2
            exit 1
        fi
        run_step cp "$source_path" "$staging"
        run_step chown 0:0 "$staging"
        run_step chmod 0644 "$staging"
    fi
    if [ "$target" = /etc/unitygateway/managed-settings.json ]; then
        run_step chown 0:0 "$staging"
        run_step chmod 0644 "$staging"
    fi

    if [ -L "$parent" ] || [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
        printf '%s\n' "Refusing to replace symlinked managed settings: $target" >&2
        exit 1
    fi
    run_step mv -f "$staging" "$target"
    staging=
    run_step restore_target_flags
    original_flags=
    clear_flags=
)

remove_one() (
    set -eu
    source_path=$1
    target=$2
    parent=${target%/*}
    if [ "$target" != /etc/unitygateway/managed-settings.json ] || ! target_is_allowed "$target"; then
        printf '%s\n' "Refusing unexpected workspace-policy removal: $target" >&2
        exit 1
    fi
    if [ -L "$parent" ] || [ -L "$target" ] || [ ! -f "$target" ]; then
        printf '%s\n' "Refusing unsafe workspace-policy removal: $target" >&2
        exit 1
    fi
    if [ ! -f "$source_path" ] || [ -L "$source_path" ]; then
        printf '%s\n' "Refusing invalid workspace-policy source: $source_path" >&2
        exit 1
    fi
    if [ "$platform" = macos ]; then
        source_owner=$(/usr/bin/stat -f %u "$source_path") || {
            printf '%s\n' "Cannot inspect workspace-policy source: $source_path" >&2
            exit 1
        }
    else
        source_owner=$(stat -c %u "$source_path") || {
            printf '%s\n' "Cannot inspect workspace-policy source: $source_path" >&2
            exit 1
        }
    fi
    if [ -z "${SUDO_UID:-}" ] || [ "$source_owner" != "$SUDO_UID" ] || ! cmp -s "$source_path" "$target"; then
        printf '%s\n' "Workspace policy changed before removal: $target" >&2
        exit 1
    fi
    rm -f "$target"
    # The folder holds only the policy; rmdir leaves it in place if anything else is there.
    rmdir "$(dirname "$target")" 2>/dev/null || true
)

replace_bundle() (
    set -eu
    source_path=$1
    target=$2
    if [ "$platform:$target" != "macos:/Library/Application Support/Claude/org-plugins/ug" ]; then
        printf '%s\n' "Refusing unexpected Desktop plugin target: $target" >&2
        exit 1
    fi
    parent=${target%/*}
    for folder in /Library "/Library/Application Support" "$parent/.." "$parent" "$target"; do
        if [ -L "$folder" ] || { [ -e "$folder" ] && [ ! -d "$folder" ]; }; then
            printf '%s\n' "Refusing linked or non-directory Desktop plugin path: $folder" >&2
            exit 1
        fi
    done
    if [ ! -d "$source_path" ] || [ -L "$source_path" ]; then
        printf '%s\n' "Invalid Desktop plugin source: $source_path" >&2
        exit 1
    fi
    source_owner=$(/usr/bin/stat -f %u "$source_path") || exit 1
    if [ -z "${SUDO_UID:-}" ] || [ "$source_owner" != "$SUDO_UID" ]; then
        printf '%s\n' "Invalid Desktop plugin source owner" >&2
        exit 1
    fi
    mkdir -p "$parent" || exit 1
    lock="$parent/.ug-install.lock"
    if ! mkdir "$lock"; then
        printf '%s\n' "Another Desktop plugin update holds $lock; retry after it finishes." >&2
        exit 1
    fi
    staging=
    moved_old=
    installed=
    committed=
    cleanup_bundle() {
        status=$?
        trap - 0 1 2 3 15
        if [ -z "$committed" ]; then
            if [ -n "$installed" ]; then rm -rf "$target"; fi
            if [ -n "$moved_old" ]; then
                if ! mv "$staging/original" "$target"; then
                    printf 'Desktop plugin backup retained at %s\n' "$staging/original" >&2
                    rmdir "$lock"
                    exit 1
                fi
            fi
        fi
        if [ -n "$staging" ]; then rm -rf "$staging"; fi
        rmdir "$lock"
        exit "$status"
    }
    trap cleanup_bundle 0
    trap 'exit 129' 1
    trap 'exit 130' 2
    trap 'exit 131' 3
    trap 'exit 143' 15
    staging=$(mktemp -d "$parent/.ug-install.XXXXXX") || exit 1
    # Copy without following links, then validate the protected snapshot before using it.
    cp -R -P "$source_path" "$staging/request" || exit 1
    invalid=$(find "$staging/request" ! -type d ! -type f -print) || exit 1
    if [ -n "$invalid" ]; then
        printf '%s\n' "Desktop plugin contains non-regular files" >&2
        exit 1
    fi
    request="$staging/request"
    if [ -e "$target" ]; then
        invalid=$(find "$target" ! -type d ! -type f -print) || exit 1
        if [ -L "$target" ] || [ -n "$invalid" ] || [ ! -d "$request/previous" ] ||
            ! diff -r "$request/previous" "$target" >/dev/null; then
            printf '%s\n' "Desktop plugin changed before installation: $target" >&2
            exit 1
        fi
    elif [ -e "$request/previous" ]; then
        printf '%s\n' "Desktop plugin disappeared before installation: $target" >&2
        exit 1
    fi
    if [ -e "$request/new" ]; then
        [ -f "$request/new/.claude-plugin/plugin.json" ] || exit 1
        chown -R 0:0 "$request/new" || exit 1
        find "$request/new" -type d -exec chmod 0755 {} + || exit 1
        find "$request/new" -type f ! -perm -0100 -exec chmod 0644 {} + || exit 1
        find "$request/new" -type f -perm -0100 -exec chmod 0755 {} + || exit 1
    fi
    if [ -e "$target" ]; then
        mv "$target" "$staging/original" || exit 1
        moved_old=1
    fi
    if [ -e "$request/new" ]; then
        mv "$request/new" "$target" || exit 1
        installed=1
    fi
    committed=1
)

case "$mode" in
    once)
        replace_one "$1" "$2"
        ;;
    remove)
        remove_one "$1" "$2"
        ;;
    session)
        while IFS=' ' read -r operation request_id source_arg target_arg; do
            case "$operation" in
                REPLACE|BUNDLE)
                    source_path=$(decode_arg "$source_arg")
                    target=$(decode_arg "$target_arg")
                    handler=replace_one
                    if [ "$operation" = BUNDLE ]; then handler=replace_bundle; fi
                    if error_output=$("$handler" "$source_path" "$target" 2>&1); then
                        printf 'OK %s\n' "$request_id"
                    else
                        status=$?
                        encoded_error=$(encode_arg "$error_output")
                        printf 'ERROR %s %s %s\n' "$request_id" "$status" "$encoded_error"
                    fi
                    ;;
                QUIT)
                    exit 0
                    ;;
                *)
                    printf 'ERROR %s 2\n' "$request_id"
                    ;;
            esac
        done
        ;;
    *)
        printf '%s\n' "Unsupported managed-settings replacement mode: $mode" >&2
        exit 2
        ;;
esac
