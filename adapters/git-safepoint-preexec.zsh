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
# NOTE: all six mirrored tables below (_GSP_VERBS / _GSP_GIT_SUBCMDS /
# _GSP_INPLACE_CMDS / _GSP_CLUSTER_FLAGS / _GSP_WRAPPERS / _GSP_WRAPPER_VALOPTS)
# are sync-checked against destructive.py by
# tests/test_destructive.py::ZshAdapterMirrorSyncTest -- edit both sides together
# or that test fails.
typeset -ga _GSP_VERBS=(rm rmdir mv truncate dd shred gzip bzip2 xz patch \
  cp rsync install)
# Sequencer commands (merge/rebase/cherry-pick/revert/am) rewind or rewrite the
# work tree via --abort/--quit/--skip and can leave a half-applied state over
# uncommitted work; apply/checkout-index write files straight into it. Fired on
# the subcommand without flag inspection, matching this table's existing policy.
typeset -ga _GSP_GIT_SUBCMDS=(checkout switch restore reset clean rm stash \
  merge rebase cherry-pick revert am apply checkout-index)
# `git worktree` only touches files for these; `list`/`add` must not fire.
typeset -ga _GSP_GIT_WORKTREE_SUBS=(remove prune move repair)
typeset -ga _GSP_INPLACE_CMDS=(sed perl awk)   # destructive only with -i
# Overwrite class gated on a CLUSTERED short option (`tar -xzf`, `ln -sf`,
# `unzip -qo`). Bare letters are short-option letters; `--` entries match whole.
# Mirrors destructive.py DESTRUCTIVE_CLUSTER_FLAG_CMDS.
typeset -gA _GSP_CLUSTER_FLAGS
_GSP_CLUSTER_FLAGS=(
  tar   "x c r u A --extract --get --create --append --update --concatenate --catenate"
  ln    "f --force"
  unzip "o"
)
# Short options that consume the REST of the token as their value, so a cluster
# scan must stop there (`tar -tvfxyz.tar`: the x is inside the file name, not a
# flag). Mirrors destructive.py _CLUSTER_VALUE_OPTS.
typeset -gA _GSP_CLUSTER_VALUE_OPTS
_GSP_CLUSTER_VALUE_OPTS=(
  tar   "f b C T X I F K s g"
  ln    "t S"
  unzip "d x"
  rsync "e f B T M"
)
# Provably-no-write modes: recognising them keeps the preexec path from rehashing
# the whole tree for a command that cannot touch it. Mirrors destructive.py
# NO_WRITE_MODES. DELIBERATELY SMALL -- a false "safe" here means no snapshot
# before real data loss. A truncating redirect is judged separately and still
# fires, so the stdout modes stay safe to recognise.
typeset -gA _GSP_NO_WRITE_LONG _GSP_NO_WRITE_LETTERS
_GSP_NO_WRITE_LONG=(
  rsync "--dry-run"
  tar   "--to-stdout"
)
_GSP_NO_WRITE_LETTERS=(
  rsync "n"
  tar   "O"
)
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
  # `${(Q)}` removes quoting exactly the way zsh's own parser does, so ADJACENT
  # quoted and unquoted fragments collapse into one word: `r'm'` -> `rm`.
  # Stripping one leading/trailing quote by hand (the previous approach) left
  # `r'm'` looking like a verb named `r'm'`, so `r'm' -rf notes` -- which really
  # runs `rm -rf notes` -- was judged SAFE and no snapshot was taken. That is the
  # dangerous direction of a detector disagreement, so it is worth matching zsh's
  # own semantics rather than approximating them.
  local v="${(Q)1}"
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

# True (0) when THIS segment carries a provably-no-write mode for its verb
# (`rsync --dry-run`, `tar -xO`). Mirrors destructive.py's _has_no_write_mode,
# which is scoped to a SINGLE segment: the caller must skip to the next segment,
# never declare the whole command safe.
#
# Declaring the locals once here (rather than inside the caller's per-token loop)
# also keeps zsh from PRINTING them: `local` on a name that already exists in the
# same scope dumps its value to the user's terminal.
_gsp_seg_has_no_write() {
  local verb="$1"
  shift
  [[ -n "${_GSP_NO_WRITE_LETTERS[$verb]}" ]] || return 1
  local _nw _nwtok _nwcluster _nwidx=0 _j _c _stopnw
  # Long options are matched in the SAME left-to-right pass that honours `--`;
  # a separate all-tokens scan would find `--dry-run` even when it sits after
  # the end-of-options marker and is therefore a file name.
  for _nwtok in "$@"; do
    [[ "$_nwtok" == "--" ]] && break
    for _nw in ${=_GSP_NO_WRITE_LONG[$verb]}; do
      [[ "$_nwtok" == "$_nw" ]] && return 0
    done
  done
  local _dashless
  for _nwtok in "$@"; do
    (( _nwidx++ ))
    # `--` ends option parsing: a later `--dry-run` is a FILE NAME.
    [[ "$_nwtok" == "--" ]] && break
    _nwcluster=""
    _dashless=0
    if [[ "$_nwtok" == --* ]]; then
      continue
    elif [[ "$_nwtok" == -* ]]; then
      _nwcluster="${_nwtok#-}"
    elif [[ "$verb" == tar && $_nwidx -eq 1 ]]; then
      # Old dashless bundle: values are separate arguments, so no letter is
      # glued on and the value-option stop must not apply (`tar gxf OLD.snar
      # backup.tar` would otherwise hide the `x`).
      _nwcluster="$_nwtok"
      _dashless=1
    else
      continue
    fi
    [[ -n "$_nwcluster" ]] || continue
    _j=1
    _stopnw=0
    while (( _j <= ${#_nwcluster} )); do
      _c="${_nwcluster[$_j]}"
      for _nw in ${=_GSP_NO_WRITE_LETTERS[$verb]}; do
        [[ "$_c" == "$_nw" ]] && return 0
      done
      if (( ! _dashless )); then
        for _nw in ${=_GSP_CLUSTER_VALUE_OPTS[$verb]}; do
          [[ "$_c" == "$_nw" ]] && _stopnw=1 && break
        done
        (( _stopnw )) && break
      fi
      [[ "$_c" == [A-Za-z] ]] || break
      (( _j++ ))
    done
  done
  return 1
}

_git_safepoint_is_destructive() {
  setopt localoptions extendedglob
  local cmd="$1"
  if _git_safepoint_has_truncating_redirect "$cmd"; then
    return 0
  fi
  # Split into segments on control operators / newlines and judge each segment's
  # head verb, so `make && rm -rf x` and multi-line scripts are seen too. (Quote
  # handling is best-effort; over-firing only costs an extra snapshot.)
  # Quote-AWARE segmentation. The previous form replaced every `;`/`&`/`|` in the
  # raw string, including ones inside quotes, so `echo 'safe; rm mentioned'` was
  # split into a bogus `rm mentioned` segment and fired. `${(z)}` splits into
  # shell words using zsh's own parser, leaving control operators as their own
  # words, so only REAL separators break a segment.
  local -a segs sw _allw _segw
  # `_gspw`, not `a`: the later `local seg verb sub a` would re-declare an
  # already-set `a`, and zsh PRINTS a re-declared local -- dumping `a=notes` to
  # stdout on every single command through preexec.
  local _gspw _redir_core _skip_redirect_target=0
  _allw=(${(z)cmd})
  segs=()
  _segw=()
  for _gspw in "${_allw[@]}"; do
    if (( _skip_redirect_target )); then
      _skip_redirect_target=0
      continue
    fi
    case "$_gspw" in
      ';'|'&&'|'||'|'|'|'&'|';;'|'|&'|$'\n')
        (( $#_segw )) && segs+=("${(j: :)${(q)_segw[@]}}")
        _segw=()
        ;;
      # `${(Q)}` here, not later: zsh's parser is what knows that `r'm'` is one
      # word meaning `rm`. Unquote at split time and re-quote with `${(q)}` when
      # rejoining, so the segment string transports words containing spaces
      # intact AND a single `${(Q)}` downstream lands on the real word.
      *)
        _redir_core="${_gspw##[0-9]#}"
        if [[ "$_gspw" == [0-9]#[\<\>\&\|]## &&
              ( "$_redir_core" == *'>'* || "$_redir_core" == *'<'* ) ]]; then
          # 先頭リダイレクトは対象も除き、実際のコマンドを先頭語にする。
          _skip_redirect_target=1
        else
          _segw+=("${(Q)_gspw}")
        fi
        ;;
    esac
  done
  (( $#_segw )) && segs+=("${(j: :)${(q)_segw[@]}}")
  # Declared ONCE for the whole function, not per segment: `local` on a name
  # that already exists in this scope makes zsh echo it, so re-declaring inside
  # the `for seg` loop dumps `_spec=f`, `_ch=f`, ... onto the user's terminal on
  # any multi-segment command (`tar -tf a.tar && tar -tf b.tar`).
  local seg verb sub a
  local _spec _tok _cluster _idx _hit _i _ch _stop _dashlessc
  for seg in $segs; do
    [[ -n "${seg// /}" ]] || continue
    _git_safepoint_strip_wrappers "$seg"
    sw=("$reply[@]")
    (( $#sw )) || continue
    # Normalize the verb BEFORE the destructive-verb / tee / inplace / git tests so
    # `\rm`, `"rm"`, `'rm'` match (mirrors destructive.py's posix-shlex).
    verb="$(_gsp_unquote "${sw[1]:t}")"
    # A provably-no-write mode (`rsync --dry-run`, `tar -xO`) makes THIS segment
    # harmless, but the command can still carry a destructive later segment
    # (`rsync -n src/ dst/ && rm -rf notes`), so skip to the next segment rather
    # than declaring the whole command safe. destructive.py scopes the identical
    # short-circuit to segment_is_destructive for exactly this reason.
    if _gsp_seg_has_no_write "$verb" "${sw[@]:1}"; then
      continue
    fi
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
    # Overwrite class gated on a clustered short option (tar -xzf / ln -sf /
    # unzip -qo). tar also accepts a dashless leading bundle (`tar xzf a.tar`),
    # read only from the FIRST argument so an ordinary all-alpha filename
    # containing the trigger letter (`tar -tf box`) cannot fire.
    if [[ -n "${_GSP_CLUSTER_FLAGS[$verb]}" ]]; then
      _idx=0
      _hit=0
      for _spec in ${=_GSP_CLUSTER_FLAGS[$verb]}; do
        for _tok in "${sw[@]:1}"; do
          [[ "$_spec" == --* && "$_tok" == "$_spec" ]] && _hit=1 && break
        done
        (( _hit )) && break
      done
      (( _hit )) && return 0
      for _tok in "${sw[@]:1}"; do
        (( _idx++ ))
        [[ "$_tok" == "--" ]] && break
        _cluster=""
        _dashlessc=0
        if [[ "$_tok" == --* ]]; then
          continue
        elif [[ "$_tok" == -* ]]; then
          _cluster="${_tok#-}"
        elif [[ "$verb" == tar && $_idx -eq 1 ]]; then
          _cluster="$_tok"
          _dashlessc=1
        else
          continue
        fi
        [[ -n "$_cluster" ]] || continue
        # 左から 1 文字ずつ見て、トリガー文字なら発火、値を食うオプション
        # (tar の f 等) に当たったらそこで打ち切る。`-xvfbackup.tar` は x が f
        # より前なので発火し、`-tvfxyz.tar` は f で止まるので発火しない。
        _i=1
        _stop=0
        while (( _i <= ${#_cluster} )); do
          _ch="${_cluster[$_i]}"
          for _spec in ${=_GSP_CLUSTER_FLAGS[$verb]}; do
            [[ "$_spec" == --* ]] && continue
            [[ "$_ch" == "$_spec" ]] && return 0
          done
          if (( ! _dashlessc )); then
            for _spec in ${=_GSP_CLUSTER_VALUE_OPTS[$verb]}; do
              [[ "$_ch" == "$_spec" ]] && _stop=1 && break
            done
            (( _stop )) && break
          fi
          # オプション束でない文字（パス・数字）に当たったら打ち切る
          [[ "$_ch" == [a-zA-Z] ]] || break
          (( _i++ ))
        done
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
      # `git worktree` only touches files for remove/prune/move/repair;
      # `list`/`add` must not force a full-tree rehash.
      if [[ "$sub" == worktree ]]; then
        for a in "${sw[@]}"; do
          if (( ${_GSP_GIT_WORKTREE_SUBS[(Ie)$a]} )); then return 0; fi
        done
      fi
    fi
  done
  return 1
}

# Print the work tree that `git worktree remove <path>` is about to delete.
#
# This is the one destructive command whose data loss happens OUTSIDE the
# current work tree, so snapshotting `--show-toplevel` would faithfully capture
# a tree that is not in danger while the doomed one -- including its dirty and
# untracked files -- goes unprotected. Returns non-zero when the command is not
# a worktree removal, leaving the caller on the current repo.
_gsp_worktree_remove_target() {
  local -a toks
  toks=(${(z)1})
  local n=$#toks i j k t sub target b saw_git
  for (( i = 1; i <= n; i++ )); do
    [[ "$(_gsp_unquote "${toks[$i]}")" == worktree ]] || continue
    # `worktree` must be THIS segment's git subcommand, not just any word on the
    # line: `echo worktree remove /tmp/wt && rm notes` would otherwise redirect
    # the snapshot to /tmp/wt and leave the `rm` unprotected. Scan back to the
    # nearest command separator and require a `git` verb in that window.
    saw_git=0
    for (( b = i - 1; b >= 1; b-- )); do
      t="$(_gsp_unquote "${toks[$b]}")"
      case "$t" in
        '&&'|'||'|';'|';;'|'|'|'|&'|'&'|$'\n') break ;;
      esac
      [[ "${t:t}" == git ]] && saw_git=1 && break
    done
    (( saw_git )) || continue
    # First non-flag word after `worktree` is the subcommand.
    sub=""
    for (( j = i + 1; j <= n; j++ )); do
      t="$(_gsp_unquote "${toks[$j]}")"
      [[ "$t" == -* ]] && continue
      sub="$t"
      break
    done
    [[ "$sub" == remove ]] || continue
    # `remove` takes exactly ONE <worktree> operand, so take the FIRST non-flag
    # word and stop at the first command separator. Taking the last one instead
    # walks straight past `&&`/`;`/`|` into the next command and picks up its
    # arguments (`git worktree remove ../wt && echo done` yielded `done`, which
    # resolves to no repo and silently fell back to snapshotting the wrong tree).
    target=""
    for (( k = j + 1; k <= n; k++ )); do
      t="$(_gsp_unquote "${toks[$k]}")"
      case "$t" in
        '&&'|'||'|';'|';;'|'|'|'|&'|'&'|$'\n') break ;;
      esac
      [[ "$t" == -* ]] && continue
      target="$t"
      break
    done
    if [[ -n "$target" ]]; then
      print -r -- "$target"
      return 0
    fi
  done
  return 1
}

git_safepoint_preexec() {
  local cmd="$1"
  # Only inside a git work tree.
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  if _git_safepoint_is_destructive "$cmd"; then
    local repo wt wtop snap_status
    repo="$(git rev-parse --show-toplevel 2>/dev/null)"
    [[ -n "$repo" ]] || return 0
    # Status 1 or 2 means the tree was not protected, so report every occurrence;
    # status 3 means an existing snapshot already covers it and is silent like 0.
    python3 "$GIT_SAFEPOINT_PY" --repo "$repo" snapshot \
      --via preexec --label "pre-shell: ${cmd}" --quiet
    snap_status=$?
    if (( snap_status == 1 || snap_status == 2 )); then
      print -u2 -r -- "warning: git-safepoint failed for '$repo'; NO protective snapshot was taken"
    fi
    # `git worktree remove <path>` destroys files in ANOTHER work tree, which the
    # snapshot above does not cover. Protect that tree IN ADDITION -- never
    # instead: a compound line can lose data on both sides
    # (`rm notes && git worktree remove ../wt`), so replacing the current repo
    # here would trade one gap for another.
    if wt="$(_gsp_worktree_remove_target "$cmd")" && [[ -n "$wt" ]]; then
      wtop="$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null)"
      if [[ -n "$wtop" && "$wtop" != "$repo" ]]; then
        python3 "$GIT_SAFEPOINT_PY" --repo "$wtop" snapshot \
          --via preexec --label "pre-shell: ${cmd}" --quiet
        snap_status=$?
        if (( snap_status == 1 || snap_status == 2 )); then
          print -u2 -r -- "warning: git-safepoint failed for worktree '$wtop'; NO protective snapshot was taken"
        fi
      fi
    fi
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
