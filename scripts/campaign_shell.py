"""The single shell projection of scripts/campaign_model.py.

Generated remote commands are self-contained, including the atomic writer:
no unchecked checkout is imported on the admission side.
"""
import argparse
import inspect
import shlex
from campaign_model import (PERMISSIONS, TRANSITIONS, UNSHARED_WRITE_BITS, BASE_ACL, atomic_write, transition, LOCK_HELD, FENCED,
                            LABEL, RUNNER_ACTIVE, FIXTURE_STATE, EPOCH, ROOT_PATH,
                            COUNTER_LIMIT, LEASE_SECONDS, LEASE_MARGIN, LEASE_WAIT_LIMIT)


def shell_umask(name):
    return f'umask {PERMISSIONS[name].umask:03o}'


def persist_command(path):
    """A shell expression for a path; payload bytes arrive only on stdin."""
    program = ('import os, sys\nfrom pathlib import Path\n' + inspect.getsource(atomic_write)
               + '\natomic_write(Path(sys.argv[1]), lambda stream: stream.write(sys.stdin.buffer.read()), '
                 'temporary=Path(sys.argv[1] + ".pending"), binary=True)\n')
    return 'python3 -c ' + shlex.quote('exec(' + repr(program) + ')') + ' ' + path


def transition_lines(machine):
    """Compile the same transition table used by the Python state machines."""
    branches = [f'{before}:{event}) state={after};;'
                for (owner, before, event), after in TRANSITIONS.items() if owner == machine]
    return ['state=NEW', 'advance() { case "$state:$1" in ' + ' '.join(branches)
            + ' *) echo "invalid campaign transition" >&2; exit 1;; esac; }']


def shell_profile(name):
    return shell_umask(name) + f'; profile_mode={PERMISSIONS[name].mode:o}'

OWNERSHIP_LINES = [
    'fail() { echo "campaign-refusal: $1" >&2; exit 1; }',
    'command -v getfacl >/dev/null || fail acl-missing',
    'owner() { local value; value=$(stat -c %u -- "$1") || fail cannot-inspect; '
    '[[ "$value" =~ ^[0-9]+$ ]] || fail cannot-inspect; printf %s "$value"; }',
    "base_acl=$'" + BASE_ACL + "'",
    'unshared() { local mode acl; mode=$(stat -c %a -- "$1") || fail cannot-inspect; '
    '[[ "$mode" =~ ^[0-7]{3,4}$ ]] || fail cannot-inspect; '
    f'(( (8#$mode & 8#{UNSHARED_WRITE_BITS:03o}) == 0 )) || fail writable-by-others; '
    'acl=$(getfacl --absolute-names --omit-header -- "$1") || fail acl-unreadable; '
    '[[ "$acl" =~ $base_acl ]] || fail acl; }',
    'trusted() { local value; [ ! -L "$1" ] && [ -d "$1" ] || fail not-a-directory; '
    'value=$(owner "$1"); { [ "$value" = 0 ] || [ "$value" = "$(id -u)" ]; } || fail foreign-owner; '
    'unshared "$1"; }',
    'owned() { local value; [ ! -L "$1" ] && { [ -d "$1" ] || [ -f "$1" ]; } '
    '|| fail not-a-file; '
    'value=$(owner "$1"); [ "$value" = "$(id -u)" ] || fail foreign-owner; unshared "$1"; }',
]

def owned_command(paths):
    """VM command that prints `owned` only if every path is a directory or
    regular file of this user, not a link, not writable by others."""
    script = '\n'.join(['set -euo pipefail', *OWNERSHIP_LINES, 'for path in "$@"; do owned "$path"; done',
                        'echo owned'])
    return 'bash -c ' + shlex.quote(script) + ' campaign-owned ' + ' '.join(shlex.quote(path) for path in paths)


def tree_unshared_command(root, prune=None):
    """Check files, directories and link targets, including owner and ACL.

    Follow links inside the root tree only; refuse dangling links, loops,
    special files and mount boundaries. `prune` has its own checks.
    """
    script = '\n'.join([
        'set -euo pipefail',
        *OWNERSHIP_LINES,
        'root=$1; prune=${2:-}',
        'boundary=$(realpath -e -- "$root" && printf .) || fail cannot-inspect',
        "boundary=${boundary%$'\\n.'}",
        'inside() { [[ "$1" = "$boundary" || "$1" = "$boundary/"* ]] || fail outside-tree; }',
        # Confinement is deliberately stricter than checking the final inode:
        # an external writable directory can replace even an owned 0600 file.
        # Check each link's spelling before realpath hides an escape/re-entry;
        # all links reached by the walk undergo this same check.
        'confined_link() { local rest cursor part resolved;',
        '  rest=$(readlink -- "$1" && printf .) || fail cannot-inspect;',
        "  rest=${rest%$'\\n.'}; cursor=${1%/*};",
        '  if [[ "$rest" = /* ]]; then inside "$rest"; rest=${rest#"$boundary"}; cursor=$boundary; fi;',
        '  while [ -n "$rest" ]; do',
        '    part=${rest%%/*}; if [[ "$rest" = */* ]]; then rest=${rest#*/}; else rest=; fi;',
        '    case "$part" in ""|.) continue;; ..) cursor=${cursor%/*};; *) cursor="$cursor/$part";; esac;',
        '    inside "$cursor";',
        '    resolved=$(realpath -e -- "$cursor" && printf .) || fail cannot-inspect;',
        "    resolved=${resolved%$'\\n.'}; inside \"$resolved\"; cursor=$resolved;",
        '  done;',
        '}',
        'device=$(stat -Lc %d -- "$root") || fail cannot-inspect',
        '[[ "$device" =~ ^[0-9]+$ ]] || fail cannot-inspect',
        'walk() { if [ -n "$prune" ]; then',
        '  find -L "$root" -xdev -path "$prune" -prune -o -print0',
        'else find -L "$root" -xdev -print0; fi; }',
        'walk | while IFS= read -r -d "" path; do',
        '  if [ -L "$path" ]; then confined_link "$path"; fi',
        '  target=$(realpath -e -- "$path" && printf .) || fail cannot-inspect',
        "  target=${target%$'\\n.'}",
        '  inside "$target"',
        '  [ "$(stat -c %d -- "$target")" = "$device" ] || fail cannot-inspect',
        '  owned "$target"',
        'done',
        'echo unshared',
    ])
    return ('bash -c ' + shlex.quote(script) + ' campaign-tree ' + shlex.quote(root)
            + (' ' + shlex.quote(prune) if prune else ''))


def admission_command(vm_root, lease=LEASE_SECONDS + LEASE_MARGIN):
    """The VM side of an admission.

    Creates the campaign directory with its first epoch if it does not exist,
    takes the exclusive lock (LOCK_HELD on contention, flock's own code for
    any other lock error), waits for the end of the previous lease, refuses
    while a labelled runner container exists (LOCK_HELD; a failing `docker ps`
    is an error), replaces the epoch and records this admission's lease of
    `lease` seconds. Then it converts to the shared lock and checks the epoch
    again: flock converts by releasing and taking again, and if another
    admission came in between, this one exits with LOCK_HELD instead of
    announcing an epoch that is no longer its own. Last it prints
    `locked <epoch> <directory>` and renews the lease on every `renew` line,
    answering `renewed`, for as long as the channel is open.
    """
    script = '\n'.join([
        'set -euo pipefail',
        'shopt -s inherit_errexit',
        shell_umask('private_file'),
        *OWNERSHIP_LINES,
        'root=$(realpath -m -- "$1")',
        'directory="$root/.campaign"',
        f'lease={int(lease)}',
        *transition_lines('admission'),
        'boot() { local id; id=$(cat /proc/sys/kernel/random/boot_id); '
        '[[ "$id" =~ ^[0-9a-f-]{36}$ ]] || { echo "unreadable boot id" >&2; exit 1; }; printf %s "$id"; }',
        'boot_clock() { local value rest; read -r value rest </proc/uptime; value=${value%%.*}; '
        '[[ "$value" =~ ^[0-9]{1,12}$ ]] || { echo "unreadable boot clock" >&2; exit 1; }; printf %s "$value"; }',
        'token() { local hex; hex=$(od -An -N16 -tx1 /dev/urandom | tr -d " \\n"); '
        '[[ "$hex" =~ ^[0-9a-f]{32}$ ]] || { echo "random source failed" >&2; exit 1; }; printf %s "$1-$hex"; }',
        'write() { printf "%s\\n" "$2" | ' + persist_command('"$directory/$1"') + '; }',
        # The value must match the pattern and the file must hold exactly it
        # and one newline, compared byte for byte: a command substitution
        # alone would drop NUL bytes and trailing newlines.
        'strict() { local value; value=$(cat "$directory/$1") || { echo "unreadable campaign $1" >&2; exit 1; }; '
        '[[ "$value" =~ $2 ]] && printf "%s\\n" "$value" | cmp -s - "$directory/$1" '
        '|| { echo "malformed campaign $1" >&2; exit 1; }; printf %s "$value"; }',
        f"epoch_pattern='^{EPOCH}$'",
        "lease_pattern='^(none|[0-9a-f-]{36} [0-9]{1,12})$'",
        'record_lease() { write lease "$(boot) $(( $(boot_clock) + 1 + lease ))"; }',
        # The root exists and was checked private (private_root_command).
        'owned "$root"',
        # First start only: the directory appears with its lock, first epoch
        # and an empty lease at once, or not at all.
        'if ! test -e "$directory" && ! test -L "$directory"; then',
        '  staging=$(mktemp -d "$root/.campaign.init.XXXXXXXX")',
        '  : >"$staging/lock"',
        '  printf "%s\\n" "$(token 0)" | ' + persist_command('"$staging/epoch"'),
        '  printf "none\\n" | ' + persist_command('"$staging/lease"'),
        '  mv -T -- "$staging" "$directory" || { rm -rf -- "$staging"; test -d "$directory"; }',
        'fi',
        # Every piece of state, checked as itself: a link anywhere stops here.
        'for path in "$directory" "$directory/lock" "$directory/epoch" "$directory/lease"; do owned "$path"; done',
        'exec 9<"$directory/lock"',
        f'flock -n -E {LOCK_HELD} -x 9',
        'advance lock',
        'previous=$(strict lease "$lease_pattern")',
        'if [ "$previous" != none ]; then',
        '  if [ "${previous%% *}" = "$(boot)" ]; then wait=$(( ${previous##* } - $(boot_clock) )); '
        'else wait=$lease; fi',
        f'  [ "$wait" -le {LEASE_WAIT_LIMIT} ] || {{ echo "recorded lease end out of range" >&2; exit 1; }}',
        '  if [ "$wait" -gt 0 ]; then echo "waiting for the end of the previous lease" >&2; sleep "$wait"; fi',
        'fi',
        'advance wait',
        f'if ! containers=$(docker ps -aq --filter "label={LABEL}=$directory" --filter status=created '
        '--filter status=running --filter status=restarting); then',
        '  echo "cannot list the runner containers" >&2; exit 1',
        'fi',
        'if [ -n "$containers" ]; then',
        f'  echo "a runner container of this VM root exists" >&2; exit {LOCK_HELD}',
        'fi',
        'advance inspect',
        'current=$(strict epoch "$epoch_pattern")',
        'counter=${current%%-*}',
        f'[ "$counter" -lt {COUNTER_LIMIT} ] || {{ echo "campaign epoch counter exhausted" >&2; exit 1; }}',
        'epoch=$(token $(( counter + 1 )))',
        'write epoch "$epoch"',
        'advance advance',
        'record_lease',
        'advance record',
        f'flock -n -E {LOCK_HELD} -s 9',
        'advance share',
        '[ "$(strict epoch "$epoch_pattern")" = "$epoch" ] '
        f'|| {{ echo "another controller was admitted during the lock conversion" >&2; exit {LOCK_HELD}; }}',
        'advance confirm',
        'echo "locked $epoch $directory"',
        'while IFS= read -r line; do',
        '  [ "$line" = renew ] || { echo "unexpected admission request" >&2; exit 1; }',
        '  record_lease',
        '  advance renew',
        '  echo renewed',
        'done',
        'advance close',
    ])
    return f'exec bash -c {shlex.quote(script)} admission {shlex.quote(vm_root)}'


def private_root_command(vm_root, top='/'):
    """The VM side of the privacy check of `vm_root`, run before the admission.

    The configured path must be canonical (absolute, no `.`, `..`, empty or
    trailing components). The check walks down from `top`, `/` for every
    campaign (tests stop it at a directory whose permissions they control):
    every directory that exists is checked, by lstat, before anything is
    looked at or created below it, and must belong to root or to this user,
    not be a link, not be writable by group or others and have no access
    control list. A level that does not exist is created with mode 700 under
    umask 077, so it is never open to others, not even for an instant; if it
    appears meanwhile, the check stops. Nothing that exists is ever changed.
    The root itself must belong to this user with mode 700. Prints `private`
    when it holds; otherwise exits 1, leaving only the private levels it
    created.
    """
    script = '\n'.join([
        'set -euo pipefail',
        shell_umask('private_file'),
        'root=$1',
        'top=$2',
        *OWNERSHIP_LINES,
        f'[[ "$root" =~ ^{ROOT_PATH}$ && "$top" =~ ^{ROOT_PATH}$ ]] '
        '|| fail characters',
        '[[ "$root" == /* && "$root" != */ && "$root/" != *//* && "$root/" != */./* && "$root/" != */../* ]] '
        '|| fail not-canonical',
        'if [ "$top" = / ]; then current=; rest=${root#/}; else',
        '  [[ "$root" == "$top"/* ]] || fail outside-top',
        '  current=$top; rest=${root#"$top"/}',
        'fi',
        'trusted "${current:-/}"',
        # Component by component with parameter expansion: no line reader.
        'while [ -n "$rest" ]; do',
        '  part=${rest%%/*}',
        '  if [ "$part" = "$rest" ]; then rest=; else rest=${rest#*/}; fi',
        '  current="$current/$part"',
        '  if test -e "$current" || test -L "$current"; then',
        '    trusted "$current"',
        '  else',
        f'    mkdir -m {PERMISSIONS["private_directory"].mode:o} -- "$current" || fail appeared',
        '  fi',
        'done',
        '[ "$current" = "$root" ] || fail not-walked',
        f'[ "$(stat -c "%u %a" -- "$root")" = "$(id -u) {PERMISSIONS["private_directory"].mode:o}" ] || fail root-mode',
        'echo private',
    ])
    return f'bash -c {shlex.quote(script)} private {shlex.quote(vm_root)} {shlex.quote(top)}'


def fence_lines(directory_variable='CAMPAIGN_DIR', epoch_variable='CAMPAIGN_EPOCH'):
    """Shell function `fence` for preparation scripts: exits with FENCED once
    the epoch file is not exactly the epoch and one newline, and with 1 if it
    cannot be read. `cmp` compares bytes; a shell variable would lose NUL
    bytes and trailing newlines."""
    return [f'fence() {{ local status=0; printf "%s\\n" "${epoch_variable}" '
            f'| cmp -s - "${directory_variable}/epoch" || status=$?; case $status in 0) ;; '
            f'1) echo "campaign epoch changed" >&2; exit {FENCED};; '
            f'*) echo "campaign epoch unreadable" >&2; exit 1;; esac; }}']


def fixture_scripts(remote_root, project, host, label, nonce, *, reset, directory, epoch):
    """The fixture preparation script of one execution, and the wrapper that runs it.

    Every execution has its own `nonce`, which names its signal files: a signal
    left by an earlier execution, of this or another campaign, is never read
    as this one's. The wrapper holds the VM campaign lock (campaign_fence)
    shared, inherited by the script and its children, so no other controller
    is admitted while the preparation lives; it also holds the campaign's
    ledger lock, so the preparation never overlaps this campaign's runner.
    When a lock is held it exits with LOCK_HELD before the script touches
    anything; any other lock error keeps its own code, and a LOCK_HELD from
    the script itself, which never refuses with it, becomes a failure. The
    script checks the admission `epoch` at its start, before changing
    fixtures and before its final record, and exits with FENCED once another
    controller was admitted. The wrapper always records the real exit code
    and exits with it. Every preparation recreates every fixture container,
    so no connection of an earlier activity survives it, and regenerates
    certificates and the SFTP fingerprint. The script ends by recording its
    nonce in FIXTURE_STATE, which the runner checks before it measures
    anything.

    A `reset` also refuses (RUNNER_ACTIVE) while a runner container of this
    campaign is running, archives the fixtures' state, logs and certificates
    first (a failed collection stops it before anything is recreated), checks
    the memory for the fixtures and then that every fixture serves requests.
    """
    q = shlex.quote

    def state_lines(state):
        return [f"printf '{{\"nonce\": \"%s\", \"kind\": \"%s\"}}\\n' {q(nonce)} {state} | "
                + persist_command(q(FIXTURE_STATE))]

    signal = '.fixtures/signals/' + label + '-' + nonce
    diagnostics = '.fixtures/diagnostics/' + label + '-' + nonce
    compose_all = 'docker compose -f docker-compose.yml -f compose.extended.yml'
    kind = 'reset' if reset else 'prepare'
    lines = ['#!/usr/bin/env bash', 'set -euo pipefail', shell_umask('fixture_file'), 'cd ' + q(remote_root),
             f'exec >{signal}.log 2>&1',
             'export COMPOSE_PROJECT_NAME=' + q(project),
             'export PLENORA_FIXTURE_HOST=' + q(host),
             *fence_lines(), 'fence']
    if reset:
        lines += [
            # A failed listing is an error, never an empty list.
            f"containers=$(docker ps --format '{{{{.Names}}}}') "
            "|| { echo 'cannot list the running containers'; exit 1; }",
            f'if grep -q {q("^" + project + "-campaign-")} <<<"$containers"; then',
            f"  echo 'a VM runner container of this campaign is still running'; exit {RUNNER_ACTIVE}",
            'fi',
            # From here on the fixtures may change: a preparation that stops
            # anywhere below leaves this state, which no runner accepts.
            'fence',
            *state_lines(transition('fixture', 'NEW', 'start')),
            f'mkdir -p {diagnostics}',
            f'{compose_all} ps --all --format json >{diagnostics}/containers.json',
            f'{compose_all} logs --no-color --timestamps >{diagnostics}/fixtures.log 2>&1',
            'for file in .fixtures/ca.crt .fixtures/minio/public.crt .fixtures/extended/server.crt '
            f'.fixtures/sftp-fingerprint; do cp "$file" {diagnostics}/; done',
            f'tar -czf {diagnostics}.tar.gz -C {diagnostics} .',
            f'python3 scripts/check_memory.py --output {signal}-memory.json',
            'fence']
    else:
        lines += ['fence', *state_lines(transition('fixture', 'NEW', 'start'))]
    lines += ['export PLENORA_FIXTURE_RECREATE=1',
              'bash scripts/prepare-fixtures.sh', 'bash scripts/prepare-extended-fixtures.sh']
    if reset:
        lines += [f'python3 scripts/check_fixtures.py --output {signal}-check.json']
    lines += ['fence', *state_lines(transition('fixture', 'in-progress', kind))]
    wrapper = (shell_umask('fixture_file') + '\n'
               f'cd {q(remote_root)} && mkdir -p .fixtures/campaign .fixtures/signals || exit 1\n'
               '(\n'
               f'  exec 9<{q(directory + "/lock")} || exit 1\n'
               f'  flock -n -E {LOCK_HELD} -s 9 || exit $?\n'
               '  exec 8>>.fixtures/campaign/campaign.lock || exit 1\n'
               f'  flock -n -E {LOCK_HELD} -x 8 || exit $?\n'
               f'  CAMPAIGN_DIR={q(directory)} CAMPAIGN_EPOCH={q(epoch)} bash .fixtures/{label}-{nonce}.sh\n'
               '  code=$?\n'
               f'  if [ "$code" -eq {LOCK_HELD} ]; then exit 1; fi\n'
               '  exit "$code"\n'
               ')\n'
               'code=$?\n'
               f'printf \'%s\\n\' "$code" | ' + persist_command(q(signal + '.exit')) + '\n'
               'exit "$code"')
    return '\n'.join(lines) + '\n', wrapper + '\n'


def checkout_command(remote_root, revision):
    """VM command that checks out `revision` from `source.bundle` in
    `remote_root`, if not done yet, and verifies it.

    It runs under umask 022: nothing of the checkout is writable by group or
    others whatever the account's umask, while the fixture containers can
    still read the files they mount. `tree_unshared_command` checks it after.
    """
    q = shlex.quote
    return (f'cd {q(remote_root)} && {shell_umask("fixture_file")} && if test ! -d .git; then git init -q && git fetch -q source.bundle HEAD '
            f'&& git checkout -q --detach FETCH_HEAD; fi && test "$(git rev-parse HEAD)" = {q(revision)} '
            '&& test -z "$(git status --porcelain --untracked-files=no)"')


def checkout_status_command(remote_root):
    return (f'cd {shlex.quote(remote_root)} && {shell_umask("private_file")} '
            '&& test -z "$(git status --porcelain)"')


def teardown_command(remote_root, project, directory, epoch):
    """Stop only this Compose project, while the admission epoch is still held."""
    q = shlex.quote
    script = '\n'.join([
        'set -euo pipefail', shell_umask('private_file'),
        f'exec 9<{q(directory + "/lock")}',
        f'flock -n -E {LOCK_HELD} -s 9',
        f'CAMPAIGN_DIR={q(directory)}; CAMPAIGN_EPOCH={q(epoch)}',
        *fence_lines(), 'fence',
        # Preparation can fail before creating the checkout.
        f'if [ -f {q(remote_root + "/docker-compose.yml")} ]; then',
        f'  cd {q(remote_root)}',
        f'  docker compose -p {q(project)} -f docker-compose.yml -f compose.extended.yml down --remove-orphans',
        'fi',
    ])
    return 'bash -c ' + q(script)


def extract_inputs_command(remote_inputs, distribution):
    """VM command that extracts the Linux distribution among the inputs and
    makes the candidate binary executable, all private to the account: modes come
    from umask 077, never from the archive, whatever the account's umask."""
    q = shlex.quote
    return (f'cd {q(remote_inputs)} && {shell_umask("private_file")} && tar --no-same-permissions -xzf linux-input.tar.gz && '
            f'chmod u+x {q(distribution + "/plenora-storage")}')


def runner_bootstrap(work, inputs, bundle, revision, fixtures):
    """The first lines of the runner container: its own source, verified.

    Copies the source bundle from the input directory `inputs` into `work`,
    in the container's own filesystem, checks the copy against the digest
    `bundle` and checks out `revision` from it; then copies every fixture file
    of `fixtures` (path under `.fixtures/` to digest) into the checkout and
    checks it. Any difference stops the container before the runner starts:
    from here on nothing is read from the host source tree.
    """
    q = shlex.quote
    lines = ['set -euo pipefail', shell_umask('fixture_file'),
             f'work={q(work)}',
             f'inputs={q(inputs)}',
             'mkdir -- "$work"',
             'cp -- "$inputs/source.bundle" "$work/source.bundle"',
             f'printf "%s  %s\\n" {q(bundle)} "$work/source.bundle" | sha256sum -c --quiet -',
             'git init -q "$work/source"',
             'git -C "$work/source" fetch -q "$work/source.bundle" HEAD',
             'git -C "$work/source" checkout -q --detach FETCH_HEAD',
             f'test "$(git -C "$work/source" rev-parse HEAD)" = {q(revision)}']
    for name, value in sorted(fixtures.items()):
        lines += [f'install -D -m {PERMISSIONS["fixture_file"].mode:o} -- "$inputs/fixtures/"{q(name)} "$work/source/.fixtures/"{q(name)}',
                  f'printf "%s  %s\\n" {q(value)} "$work/source/.fixtures/"{q(name)} | sha256sum -c --quiet -']
    lines.append('test -z "$(git -C "$work/source" status --porcelain)"')
    return lines



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=PERMISSIONS)
    parser.add_argument('--mode', action='store_true')
    args = parser.parse_args()
    print(f'{PERMISSIONS[args.profile].mode:o}' if args.mode else shell_profile(args.profile))
