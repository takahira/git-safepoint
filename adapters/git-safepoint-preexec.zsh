# git-safepoint zsh preexec adapter (template)
#
# Source this from ~/.zshrc to snapshot the current git repo right before a
# destructive command runs in your shell. It mirrors the PreToolUse hook: a
# thin trigger over the single capture engine. It NEVER blocks your command --
# the snapshot runs, then the command proceeds (fail-open).
#
#   source /ABS/PATH/adapters/git-safepoint-preexec.zsh
#
# Point GIT_SAFEPOINT_PY at the standalone entry point:
#   export GIT_SAFEPOINT_PY=/ABS/PATH/git_safepoint.py
#
# Status: the preexec wiring itself is a documented template (its live behaviour
# in an interactive zsh is gated -- not exercised in the offline test suite).
# The destructive-verb judgement and the capture path it calls ARE tested.

: "${GIT_SAFEPOINT_PY:=$HOME/git-safepoint/git_safepoint.py}"

# First-token verbs worth a pre-snapshot. Kept in sync with git-safepoint's
# authoritative head-verb allowlist in git_safepoint/destructive.py. Over-firing
# (an extra snapshot) is harmless; a miss leaves you unprotected, so when in
# doubt this list errs toward firing.
# NOTE: all five mirrored tables below (_GSP_VERBS / _GSP_GIT_SUBCMDS /
# _GSP_INPLACE_CMDS / _GSP_WRAPPERS / _GSP_WRAPPER_VALOPTS) are sync-checked
# against destructive.py by tests/test_destructive.py::ZshAdapterMirrorSyncTest --
# edit both sides together or that test fails.
typeset -ga _GSP_VERBS=(rm rmdir mv truncate dd shred gzip bzip2 xz patch)
typeset -ga _GSP_GIT_SUBCMDS=(checkout switch restore reset clean rm stash)
typeset -ga _GSP_INPLACE_CMDS=(sed perl awk)   # destructive only with -i
typeset -ga _GSP_WRAPPERS=(env sudo doas time nohup command exec builtin \
  nice xargs timeout stdbuf setsid ionice)
# wrapper -> space-padded list of options that consume a following value token,
# so `sudo -u root rm` strips to `rm` (not `root`). Mirrors destructive.py
# _WRAPPER_VALUE_OPTS exactly, including the long forms (`--user`, `--adjustment`,
# `--unset`, ...) so e.g. `sudo --user root rm x` does not read `root` as the verb.
typeset -gA _GSP_WRAPPER_VALOPTS
_GSP_WRAPPER_VALOPTS=(
  sudo    " -u -g -U -p -C -r -t -h --user --group --prompt --role --type "
  doas    " -u -C "
  nice    " -n --adjustment "
  ionice  " -c -n -p --class --classdata --pid "
  stdbuf  " -i -o -e --input --output --error "
  timeout " -s -k --signal --kill-after "
  xargs   " -I -L -n -P -s -d -E -a --replace --max-lines --max-args --max-procs --delimiter --arg-file --eof "
  env     " -u -S -C --unset --split-string --chdir "
)

# Normalize a candidate verb token to match destructive.py's posix-shlex parsing:
# strip a single leading backslash (`\rm` -> `rm`) and a single matched pair of
# surrounding quotes (`"rm"`/`'rm'` -> `rm`). Without this the literal backslash /
# quote chars survive zsh's `${(z)...}` split and the verb never matches.
_gsp_unquote() {
  local v="$1"
  v="${v#\\}"
  v="${v#[\"\']}"
  v="${v%[\"\']}"
  print -r -- "$v"
}

_git_safepoint_strip_wrappers() {
  # Set `reply` to the command words after peeling leading wrappers, their
  # flags+values, and VAR=value prefixes. Mirrors destructive.py _strip_wrappers
  # so `sudo -u root rm`, `nice -n 10 rm`, `timeout 5 rm` all strip to `rm ...`.
  local -a words
  words=(${(z)1})
  local i=1 w base valopts opt
  while (( i <= $#words )); do
    w="${words[i]}"
    base="$(_gsp_unquote "${w:t}")"   # normalize so `\sudo` / `"sudo"` peel too
    # Peel leading shell reserved words / compound-command prefixes (then/do/
    # else/elif/{/!) so a destructive verb in an if/for/while body or brace group
    # is judged. Mirrors destructive.py _strip_compound_prefixes.
    case "$w" in
      then|do|else|elif|'{'|'!') (( i += 1 )); continue ;;
    esac
    if (( ${_GSP_WRAPPERS[(Ie)$base]} )); then
      valopts="${_GSP_WRAPPER_VALOPTS[$base]}"
      (( i += 1 ))
      while (( i <= $#words )) && [[ "${words[i]}" == -* ]]; do
        opt="${words[i]}"
        (( i += 1 ))
        if [[ "$opt" != *=* && -n "$valopts" && "$valopts" == *" $opt "* ]]; then
          (( i += 1 ))   # skip the option's value
        fi
      done
      # timeout's positional DURATION (starts with a digit) precedes the command.
      # This [0-9]* heuristic is looser than Python's _looks_like_duration (which
      # requires a full number+unit), so e.g. `timeout 7z x` over-eats `7z`. Harmless:
      # no destructive verb starts with a digit, so over-eating a
      # digit-leading token only ever fails to fire on a NON-destructive command.
      if [[ "$base" == timeout ]] && (( i <= $#words )) && [[ "${words[i]}" == [0-9]* ]]; then
        (( i += 1 ))
      fi
      continue
    fi
    # VAR=value env assignment prefix.
    if [[ "$w" == *=* && "$w" != -* && "${w%%=*}" != */* ]]; then
      (( i += 1 )); continue
    fi
    break
  done
  reply=("${words[@]:$((i-1))}")
}

_git_safepoint_git_sub() {
  # Echo git's effective subcommand from already-wrapper-stripped words ($@),
  # where $1 == git; skip git's global options and their values.
  local -a w; w=("$@")
  local i=2 t
  while (( i <= $#w )); do
    t="$w[i]"
    case "$t" in
      -C|-c|--git-dir|--work-tree|--namespace) (( i += 2 )); continue ;;
      -*) (( i += 1 )); continue ;;
      *) print -r -- "$t"; return ;;
    esac
  done
}

_git_safepoint_has_truncating_redirect() {
  # Fire on a file-truncating redirect but NOT append (`>>`/`&>>`), fd-dup
  # (`>&N`/`N>&M`), input (`<`/`<>`), or a redirect to /dev/null. Mirrors
  # destructive.py _has_truncating_redirect: tokenise with `${(z)...}` (which
  # splits the redirect OPERATOR from its TARGET even when glued, e.g.
  # `>/dev/null` -> `>` `/dev/null`, `>&2file` -> `>&` `2file`), then compare the
  # target token for EXACT equality, so a real file whose name merely STARTS WITH
  # `/dev/null` (`> /dev/nullx`, `> /dev/null.bak`) still fires. (Quoted `>` is
  # NOT a concern here -- ${(z)...} only emits an operator token for an UNQUOTED
  # redirect glyph; the Python engine is authoritative once we fork.)
  setopt localoptions extendedglob
  local -a words
  words=(${(z)1})
  local i op core tgt
  for (( i = 1; i <= $#words; i++ )); do
    op="${words[i]}"
    # A redirect operator token is only `<>&|` plus an optional leading fd digit
    # prefix (`2>`, `1>&`). Anything else (a command word, a filename) is skipped.
    [[ "$op" == [0-9]#[\<\>\&\|]## ]] || continue
    # Strip the fd-number prefix so `2>&`->`>&`, `2>`->`>`, `2>|`->`>|`, leaving a
    # bare operator core. (`&>`/`&>>`->`>>&` have a `&` prefix, kept verbatim.)
    core="${op##[0-9]#}"
    tgt="${words[i+1]}"
    case "$core" in
      # fd-dup OR both-streams-to-file: `>&`, `N>&`. Python fires unless the
      # target is purely numeric (`2>&1`/`>&2` fd-dup) or /dev/null. A
      # digit-LEADING but non-numeric target (`>&2file`) is a FILENAME -> fires.
      '>&')
        [[ -n "$tgt" && "$tgt" != /dev/null && "$tgt" != <-> ]] && return 0
        ;;
      # truncating-to-file: `>`, `>|`, `&>` (and fd-prefixed `2>`, `2>|`). Fire
      # unless the target is exactly /dev/null. Append `>>`/`&>>`(`>>&`) and input
      # `<`/`<>` fall through to the default (no fire).
      '>'|'>|'|'&>')
        [[ -n "$tgt" && "$tgt" != /dev/null ]] && return 0
        ;;
    esac
  done
  return 1
}

_git_safepoint_is_destructive() {
  local cmd="$1"
  if _git_safepoint_has_truncating_redirect "$cmd"; then
    return 0
  fi
  # Split into segments on control operators / newlines and judge each segment's
  # head verb, so `make && rm -rf x` and multi-line scripts are seen too. (Quote
  # handling is best-effort; over-firing only costs an extra snapshot.)
  local nl=$'\n'
  local -a segs sw
  segs=("${(@f)${cmd//[;&|]/$nl}}")
  local seg verb sub a
  for seg in $segs; do
    [[ -n "${seg// /}" ]] || continue
    _git_safepoint_strip_wrappers "$seg"
    sw=("$reply[@]")
    (( $#sw )) || continue
    # Normalize the verb BEFORE the destructive-verb / tee / inplace / git tests so
    # `\rm`, `"rm"`, `'rm'` match (mirrors destructive.py's posix-shlex).
    verb="$(_gsp_unquote "${sw[1]:t}")"
    if (( ${_GSP_VERBS[(Ie)$verb]} )); then
      return 0
    fi
    # tee truncates its file ARGUMENTS unless -a/--append. A bare `tee` (or only
    # `tee /dev/null`) is a harmless pass-through, so require a real file target
    # before firing.
    if [[ "$verb" == tee ]]; then
      if (( ! ${sw[(Ie)-a]} )) && (( ! ${sw[(Ie)--append]} )); then
        local _t _hastgt=0
        for _t in "${sw[@]:1}"; do
          [[ "$_t" == -* || "$_t" == /dev/null ]] && continue
          _hastgt=1; break
        done
        (( _hastgt )) && return 0
      fi
    fi
    # In-place editors: sed -i / perl -i / awk -i (and glued forms like -i.bak).
    if (( ${_GSP_INPLACE_CMDS[(Ie)$verb]} )); then
      for a in "${sw[@]:1}"; do
        case "$a" in
          -i|-i*|--in-place|--in-place=*) return 0 ;;
        esac
      done
    fi
    if [[ "$verb" == git ]]; then
      sub="$(_gsp_unquote "$(_git_safepoint_git_sub "$sw[@]")")"
      if (( ${_GSP_GIT_SUBCMDS[(Ie)$sub]} )); then
        return 0
      fi
      # `git branch -D/-d/--delete` clobbers refs.
      if [[ "$sub" == branch ]]; then
        for a in "${sw[@]}"; do
          case "$a" in
            -D|-d|--delete) return 0 ;;
          esac
        done
      fi
    fi
  done
  return 1
}

git_safepoint_preexec() {
  local cmd="$1"
  # Only inside a git work tree.
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  if _git_safepoint_is_destructive "$cmd"; then
    local repo
    repo="$(git rev-parse --show-toplevel 2>/dev/null)"
    [[ -n "$repo" ]] || return 0
    python3 "$GIT_SAFEPOINT_PY" --repo "$repo" snapshot \
      --via preexec --label "pre-shell: ${cmd}" --quiet 2>/dev/null
  fi
  return 0  # never block the command
}

autoload -Uz add-zsh-hook 2>/dev/null
if command -v add-zsh-hook >/dev/null 2>&1; then
  add-zsh-hook preexec git_safepoint_preexec
else
  # Fallback for older zsh without add-zsh-hook.
  preexec_functions+=(git_safepoint_preexec)
fi
