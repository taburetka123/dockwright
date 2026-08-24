"""bootstrap-recreate.sh account stamping must read the registry (any pool
account name), not the hardcoded a/b pair (F4) — a custom account name gets
no CONFIG_PREFIX stamp and silently rides the default login.

Also pins the incident guard (2026-07-17): the actual 08:41 vector was an AGENT
hand-running the script under a sandboxed HOME — and -L namespaces tmux by uid,
not HOME, so it spawned two rogue managers onto the LIVE socket. The script now:
  * --dry-run                    → prints the plan, exits before any spawn;
  * sandboxed HOME + live socket → REFUSED (exit 3), naming --dry-run;
  * sandboxed HOME + scratch sock→ still spawns (the legitimate test shape — the
                                   guard's socket gate must not over-fire).
All run the REAL script and are self-contained in safety — each prepends its OWN
fake-tmux dir to PATH, so any spawn tail hits a logging stub, never real tmux,
even if the conftest autouse shim is ever removed."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "bootstrap-recreate.sh"


def test_bootstrap_recreate_stamps_from_registry_not_ab_hardcode():
    text = SCRIPT.read_text()
    executed = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("#"))
    assert '"$ACTIVE_LETTER" = "a"' not in executed      # the old hardcode
    assert 'account-registry.json' in executed


def _fake_tmux_dir(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir()
    log = tmp_path / "tmux-invocations.log"
    (d / "tmux").write_text(
        "#!/bin/bash\n"
        f"echo \"ENV_SKIP=${{DOCKWRIGHT_MANAGER_SKIP_PERMS:-unset}} $@\" >> {log}\n"
        "case \"$*\" in *has-session*) exit 1 ;; *new-session*|*new-window*) echo '@1'; exit 0 ;; esac\n"
        "exit 0\n")
    (d / "tmux").chmod(0o755)
    (d / "jq").symlink_to(shutil.which("jq"))
    (d / "uuidgen").symlink_to(shutil.which("uuidgen"))
    return d, log


def _seed_predecessor(home, name="mighty-demon", domain="personal", sid="sid-x",
                      agent="manager"):
    """Predecessor's active record — the script resolves the successor's
    manager_name + domain from it. name=None / domain=None omits that key
    entirely (the field-absent shapes the fail-loud tests pin); agent lets a
    test seed a non-manager record (which must never donate an identity)."""
    active = home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True, exist_ok=True)
    record = {"claude_sid": sid, "agent": agent, "name": name,
              "domain": domain, "pid": 4242}
    if name is None:
        del record["name"]
    if domain is None:
        del record["domain"]
    (active / f"{sid}.json").write_text(json.dumps(record))


def _handoff_leftovers(home):
    handoffs = home / ".claude" / "dockwright" / "handoffs"
    if not handoffs.exists():
        return []
    return list(handoffs.glob("*.json")) + list(handoffs.glob("*.tmp"))


def _error_fields_segment(stderr):
    """The missing-fields ERROR line, truncated before the sid and the probed
    path — both the tmp_path embedded via `(probed …)` and a caller-supplied
    from-sid can contain a field-name word (e.g.
    .../test_record_without_domain_fai0/..., `sid-domain-x`), so field-name
    assertions bind to the message segment only."""
    error_line = next(l for l in stderr.splitlines() if l.startswith("ERROR:"))
    return error_line.split(" for predecessor", 1)[0]


def _run_bootstrap(tmp_path, fakebin, *extra, env_overrides=None, seed=True):
    """seed: True = full predecessor record, False = none, or a kwargs dict
    forwarded to _seed_predecessor (e.g. {"domain": None})."""
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    if seed is True:
        _seed_predecessor(home)
    elif seed:
        _seed_predecessor(home, **seed)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("DOCKWRIGHT_MANAGER_RC", None)
    env.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x", *extra],
        capture_output=True, text=True, env=env), home


def test_dry_run_probes_without_spawning(tmp_path):
    """--dry-run must not mutate: no tmux AND no handoff file (it used to
    write one — today's orphan 11a7fb6c… incident evidence). The payload it
    WOULD write is printed as `handoff_payload: {...}` and must carry the
    seeded predecessor's identity."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "DRY_RUN: no spawn." in r.stdout
    assert not log.exists(), f"--dry-run still reached tmux: {log.read_text()}"
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"--dry-run wrote handoff files: {leftovers}"
    payload_line = next(l for l in r.stdout.splitlines()
                        if l.startswith("handoff_payload: "))
    payload = json.loads(payload_line[len("handoff_payload: "):])
    assert payload["manager_name"] == "mighty-demon", payload
    assert payload["domain"] == "personal", payload


def test_real_run_handoff_carries_predecessor_identity(tmp_path):
    """T1: a real (non-dry) run stamps the predecessor's manager_name + domain
    into the written handoff — the two keys whose absence made the takeover
    silently re-roll the successor's name (stranding its workers) and default
    its domain to general. Scratch socket keeps the caged spawn legitimate.
    --reason rides along so a dropped inner dispatch arm for it (the parser's
    accept-list and assign-list are separate) reds here instead of silently
    degrading to the default."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-identity"
    r, home = _run_bootstrap(tmp_path, fakebin, "--reason", "probe-reason",
                             env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock})
    assert r.returncode == 0, r.stderr
    handoffs = list((home / ".claude" / "dockwright" / "handoffs").glob("*.json"))
    assert len(handoffs) == 1, handoffs
    payload = json.loads(handoffs[0].read_text())
    assert payload["trigger_reason"] == "probe-reason", payload
    assert payload["manager_name"] == "mighty-demon", payload
    assert payload["domain"] == "personal", payload


def test_missing_predecessor_record_fails_before_spawn_and_before_sandbox_guard(tmp_path):
    """T3: no active record, no flags → exit 4 naming both fields, with NO
    handoff written and NO tmux call. Sandbox HOME + DEFAULT socket must
    yield 4, not 3 — identity resolution fires BEFORE the sandbox guard.
    Field-name assertions bind to the ERROR line's message segment: the remedy
    prose below it mentions both fields unconditionally, so a whole-stderr
    match would stay green no matter which fields MISSING actually names."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, seed=False)
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment and "domain" in segment, segment
    assert "--manager-name" in r.stderr, r.stderr
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"exit-4 path still wrote a handoff: {leftovers}"
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_dry_run_with_unresolvable_identity_fails_loud(tmp_path):
    """Identity resolution fires BEFORE the dry-run exit: a probe against an
    unresolvable predecessor must exit 4 like the real run would, not report a
    spawn plan (with empty identity fields) that the real run could never
    execute. Crosses the two axes the neighboring tests cover separately —
    the exit-4 tests all run non-dry, the dry-run tests all seed a record."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, "--dry-run", seed=False)
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stdout}{r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment and "domain" in segment, segment
    assert "handoff_payload:" not in r.stdout, r.stdout
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"dry-run exit-4 path wrote handoff files: {leftovers}"
    assert not log.exists(), f"dry-run exit-4 path reached tmux: {log.read_text()}"


def test_explicit_overrides_rescue_missing_record(tmp_path):
    """T4: --manager-name/--domain rescue a reaped record (the incident shape:
    _prune_stale_active_records already unlinked the dead manager's record) —
    the spawn proceeds and the handoff carries the explicit identity."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-override"
    r, home = _run_bootstrap(
        tmp_path, fakebin,
        "--manager-name", "mighty-demon", "--domain", "personal",
        env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock}, seed=False)
    assert r.returncode == 0, r.stderr
    invocations = log.read_text() if log.exists() else ""
    assert "/manager-resume" in invocations, invocations
    handoffs = list((home / ".claude" / "dockwright" / "handoffs").glob("*.json"))
    assert len(handoffs) == 1, handoffs
    payload = json.loads(handoffs[0].read_text())
    assert payload["manager_name"] == "mighty-demon", payload
    assert payload["domain"] == "personal", payload


def test_explicit_flags_beat_record(tmp_path):
    """Flag-vs-record precedence: an explicit flag must beat a present record
    for BOTH fields — inverted precedence would silently ignore the operator's
    deliberate override while everything still exits 0."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-precedence"
    r, home = _run_bootstrap(
        tmp_path, fakebin,
        "--manager-name", "other-name", "--domain", "other-domain",
        env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock})
    assert r.returncode == 0, r.stderr
    handoffs = list((home / ".claude" / "dockwright" / "handoffs").glob("*.json"))
    assert len(handoffs) == 1, handoffs
    payload = json.loads(handoffs[0].read_text())
    assert payload["manager_name"] == "other-name", payload
    assert payload["domain"] == "other-domain", payload


def _case_label_flags():
    """Every `--flag` in a case-arm-label position ANYWHERE in the script's
    comment-stripped text: `(?:^|;;)\\s*flag[|flag...]\\)` per line, split on
    alternations. Deliberately NOT a classifier — no loop bounding, no `shift`
    counting, no depth tracking. The previous derivation parser classified
    arms as value-taking and was itself fail-open: four ADD-ONE shapes
    (`shift; shift`, `shift  2`, `shift 2` inside a nested case, a digit in
    the flag name) each re-opened the live-spawn hole while every test stayed
    green, because partial classification blindness returned exactly the known
    flags and the == meta passed. Enumeration can only OVER-collect, and
    over-collection ADDS driven test cases — the fail-safe direction."""
    lines = [l for l in SCRIPT.read_text().splitlines()
             if not l.lstrip().startswith("#")]
    flags = set()
    for line in lines:
        for m in re.finditer(r"(?:^|;;)\s*((?:--[\w-]+\|)*--[\w-]+)\)", line):
            flags.update(m.group(1).split("|"))
    return flags


def _arg_parse_block():
    """Comment-stripped text of the script's argv loop: the line containing
    `while [ $# -gt 0 ]` through the first bare `done`, rstrip'd."""
    lines = [l for l in SCRIPT.read_text().splitlines()
             if not l.lstrip().startswith("#")]
    i = next(n for n, l in enumerate(lines) if "while [ $# -gt 0 ]" in l)
    j = next(n for n in range(i, len(lines)) if lines[n].strip() == "done")
    return "\n".join(l.rstrip() for l in lines[i:j + 1])


EXPECTED_BLOCK = '''\
while [ $# -gt 0 ]; do
    case "$1" in
        --narrative|--from-sid|--reason|--manager-name|--domain)
            if [ $# -lt 2 ] || [ "${2#--}" != "$2" ]; then
                echo "ERROR: $1 requires a value (got '${2:-}')" >&2
                echo "Usage: $0 --narrative <prose> --from-sid <sid> [--manager-name <name>] [--domain <domain>] [--reason <string>] [--dry-run]" >&2
                exit 2
            fi
            case "$1" in
                --narrative) NARRATIVE="$2" ;;
                --from-sid) FROM_SID="$2" ;;
                --reason) REASON="$2" ;;
                --manager-name) MANAGER_NAME="$2" ;;
                --domain) DOMAIN="$2" ;;
                *) echo "internal: unhandled value flag $1" >&2; exit 2 ;;
            esac
            shift 2 ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        *)
            echo "ERROR: unknown arg '$1'" >&2
            echo "Usage: $0 --narrative <prose> --from-sid <sid> [--manager-name <name>] [--domain <domain>] [--reason <string>] [--dry-run]" >&2
            exit 2 ;;
    esac
done'''


def test_arg_parse_block_unchanged():
    """The snapshot forces a human look at ANY argv-handling change: label
    shapes the enumeration cannot parse — short aliases (`-a|--account)`),
    globs, quoted or `$VAR`-expanded labels — silently SHRINK the driven set
    (fail-open), so the enumerate-and-drive matrix alongside proves behavior
    is still safe only for the labels it could see. On a legitimate edit:
    re-derive by hand — confirm every new arm routes through the guarded
    shared arm AND appears in test_no_flag_can_swallow_dry_run's matrix —
    then re-bless this literal. That re-blessing friction is the feature,
    not a broken test to delete."""
    assert _arg_parse_block() == EXPECTED_BLOCK, (
        "bootstrap-recreate.sh's argv loop changed — see this test's "
        "docstring: re-derive the guard coverage by hand, then re-bless "
        "EXPECTED_BLOCK")


def test_value_flags_meta():
    """META (drift-guard delete-one + ADD-ONE, recursive): the enumerated
    flag set is pinned with == — never >= — so a vanished flag, a new flag,
    and an empty set (enumeration broke) all fail loud. If this reds on a NEW
    flag: if it takes a value, route it through the guarded shared arm
    (missing / `--*`-shaped value rejection), then update this literal —
    test_no_flag_can_swallow_dry_run drives the new flag automatically either
    way. Second half (D-3): the shared arm's inner dispatch must keep its `*)`
    internal-error catch-all — a flag in the accept-list but not dispatched
    would otherwise be consumed and silently ignored; asserted on the
    comment-stripped (executed) text."""
    enumerated = _case_label_flags()
    assert enumerated == {"--narrative", "--from-sid", "--reason",
                          "--manager-name", "--domain", "--dry-run"}, (
        f"case-arm flags enumerated from bootstrap-recreate.sh: "
        f"{sorted(enumerated)} — see this test's docstring for what to do")
    executed = "\n".join(l for l in SCRIPT.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
    assert '*) echo "internal: unhandled value flag $1" >&2; exit 2 ;;' in executed, (
        "the shared value-flag arm's inner dispatch lost its `*)` "
        "internal-error catch-all")


@pytest.mark.parametrize("flag", sorted(_case_label_flags()))
def test_no_flag_can_swallow_dry_run(tmp_path, flag):
    """THE safety property, asserted behaviorally for every enumerated flag:
    `<flag> --dry-run` must never reach tmux and never write a handoff,
    whatever <flag> does with its argument. Seeded record + SCRATCH socket on
    purpose: identity resolves and the sandbox guard is silent, so the ONLY
    thing between a flag that swallows --dry-run and a spawn is the parser
    guard under test — the exact 2026-07-17 believed-probe live-spawn shape.
    Exit code deliberately unasserted: guarded value flags exit 2 while
    `--dry-run --dry-run` legitimately dry-runs at exit 0; the property is
    no-spawn-no-write, not a specific code."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-swallow"
    r, home = _run_bootstrap(tmp_path, fakebin, flag, "--dry-run",
                             env_overrides={"DOCKWRIGHT_TMUX_SOCKET": sock})
    assert not log.exists(), (
        f"`{flag} --dry-run` reached tmux (the flag swallowed the probe flag "
        f"and the run spawned): {log.read_text()}")
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"`{flag} --dry-run` wrote a handoff: {leftovers}"


VALUE_FLAGS = ["--narrative", "--from-sid", "--reason", "--manager-name", "--domain"]


@pytest.mark.parametrize("shape", ["flag-shaped-value", "argv-end"])
@pytest.mark.parametrize("flag", VALUE_FLAGS)
def test_value_flag_missing_value_rejected(tmp_path, flag, shape):
    """The five KNOWN value flags reject a missing value or a `--*`-shaped
    one with a usage error: exit 2, nothing written, no tmux — the strict
    per-flag contract for the flags the guard was written against (a
    deliberately literal list). The any-flag-ever property — nothing can
    swallow --dry-run into a live spawn — lives in
    test_no_flag_can_swallow_dry_run + test_value_flags_meta, which enumerate
    the script's case-arm labels instead of classifying them.
    The guard runs per occurrence, so the malformed repeat of a flag
    _run_bootstrap already passed correctly is still rejected."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    extra = [flag, "--dry-run"] if shape == "flag-shaped-value" else [flag]
    r, home = _run_bootstrap(tmp_path, fakebin, *extra)
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}: {r.stderr}"
    assert "value" in r.stderr and "Usage:" in r.stderr, r.stderr
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-2 path still reached tmux: {log.read_text()}"


def test_worker_record_never_donates_identity(tmp_path):
    """The record consult is gated on .agent == "manager": a typo'd --from-sid
    hitting a WORKER's sid must not inherit the worker's name/domain as the
    successor manager's identity — exit 4 naming both fields instead. Green
    against pristine code; red-proven by neutering the RECORD_AGENT gate in a
    scratch copy (the one line the mutation sweep previously missed)."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin,
                             seed={"name": "busy-otter", "agent": "worker"})
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment and "domain" in segment, segment
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_record_without_domain_fails_loud(tmp_path):
    """T5: a manager record LACKING `domain` must not yield a domain-less
    handoff (the takeover would previously have defaulted it to general — the
    wrong-domain latent bug). The ERROR message segment names only domain;
    the remedy lines legitimately mention --manager-name and the probed path
    embeds a tmp_path that contains the word 'domain', so both assertions
    bind to the pre-`(probed` segment."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, seed={"domain": None})
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "domain" in segment, segment
    assert "manager_name" not in segment, segment
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_record_without_name_fails_loud(tmp_path):
    """Mirror of T5 for the other identity field: a manager record LACKING
    `name` must not yield a handoff with manager_name:"" — the successor would
    re-roll a fresh identity and strand every in-flight worker (the incident
    itself: 24 orphaned events in done/mighty-demon/). Red-provable pin on the
    manager_name half of the -z gate, which no other test isolates."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin, seed={"name": None})
    assert r.returncode == 4, f"expected exit 4, got {r.returncode}: {r.stderr}"
    segment = _error_fields_segment(r.stderr)
    assert "manager_name" in segment, segment
    assert "domain" not in segment, segment
    assert not _handoff_leftovers(home)
    assert not log.exists(), f"exit-4 path still reached tmux: {log.read_text()}"


def test_sandboxed_home_live_socket_is_refused(tmp_path):
    """This IS the 2026-07-17 08:41 incident shape, now refused instead of
    spawning: an agent hand-ran the script under a sandboxed HOME against the
    live/default socket, and -L namespaces tmux by uid not HOME, so it spawned
    onto the live fleet. The guard refuses it (exit 3, naming --dry-run) BEFORE
    any tmux call — the fake-tmux log stays absent.
    RED against the unfixed script: no guard → the spawn tail reached tmux
    (`-L dockwright` + `/manager-resume` in the log), exit != 3 (still caged by
    the test-local fake tmux)."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    r, home = _run_bootstrap(tmp_path, fakebin)
    assert r.returncode == 3, f"expected refusal exit 3, got {r.returncode}: {r.stderr}"
    assert "--dry-run" in r.stderr, r.stderr
    assert not log.exists(), f"refusal still reached tmux: {log.read_text()}"
    leftovers = _handoff_leftovers(home)
    assert not leftovers, f"refused run still wrote handoff files: {leftovers}"


def test_sandboxed_home_scratch_socket_still_spawns(tmp_path):
    """The guard's socket gate is a deliberate refinement over a bare HOME check:
    a sandboxed HOME against an EXPLICIT scratch socket is a legitimate test shape
    (test_gardener_run_tmux does it), so the guard fires only on sandbox-HOME +
    live/default socket. This preserves the old test's spawn-tail pin (the tail
    reaches tmux) AND proves the socket gate does not over-fire."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-probe"
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    _seed_predecessor(home)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
           "DOCKWRIGHT_TMUX_SOCKET": sock}
    subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x"],
        capture_output=True, text=True, env=env)
    invocations = log.read_text() if log.exists() else ""
    assert f"-L {sock}" in invocations and "/manager-resume" in invocations, invocations


def test_dry_run_cmd_carries_remote_control(tmp_path):
    """The composed RUNTIME_CMD — printed VERBATIM by --dry-run as cmd=[…] —
    carries --remote-control before the /manager-resume prompt. Anchored to
    the executed command, not script prose (drift-guard discipline); same
    tail as manager_launch.manager_claude_args()."""
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control" in cmd, cmd
    assert cmd.index("--remote-control") < cmd.index("/manager-resume"), cmd
    # Parse-shape invariant: --remote-control adjacent to the --model dash-option
    # (never the trailing /manager-resume prompt, which --remote-control [name]
    # would otherwise bind as the RC session name).
    assert "--remote-control --model" in cmd, cmd


def test_dry_run_cmd_rc_opt_out(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_RC": "0"})
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control" not in cmd, cmd


def test_dry_run_cmd_carries_skip_perms_opt_in(tmp_path):
    """RUNTIME_CMD (printed VERBATIM by --dry-run as cmd=[…]) carries the flag
    when DOCKWRIGHT_MANAGER_SKIP_PERMS=1 — anchored to the executed command,
    not script prose. Adjacency pins the parse shape: RC, then skip, then the
    --model dash-option."""
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run",
                              env_overrides={"DOCKWRIGHT_MANAGER_SKIP_PERMS": "1"})
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--remote-control --dangerously-skip-permissions --model" in cmd, cmd
    assert cmd.index("--dangerously-skip-permissions") < cmd.index("/manager-resume"), cmd


def test_dry_run_cmd_skip_perms_default_off(tmp_path):
    fakebin, _log = _fake_tmux_dir(tmp_path)
    r, _home = _run_bootstrap(tmp_path, fakebin, "--dry-run")
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    assert "--dangerously-skip-permissions" not in cmd, cmd


def test_spawn_env_scrubbed_but_cmd_carries_flag(tmp_path):
    """Server-birth stickiness guard (spec § Server-birth scrub): the script
    unsets the var AFTER composing RUNTIME_CMD, so the tmux invocation (which
    may birth the server) sees a clean env while the spawned command line still
    carries the one-shot flag. The fake tmux logs ENV_SKIP=<value-or-unset>
    per invocation. Scratch socket: the sandbox-HOME guard only permits
    non-dry-run spawns on an explicit non-live socket."""
    fakebin, log = _fake_tmux_dir(tmp_path)
    sock = f"wt-iso-{os.getpid()}-skip"
    home = tmp_path / "home"
    (home / ".claude" / "dockwright").mkdir(parents=True)
    _seed_predecessor(home)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
           "DOCKWRIGHT_TMUX_SOCKET": sock,
           "DOCKWRIGHT_MANAGER_SKIP_PERMS": "1"}
    env.pop("DOCKWRIGHT_MANAGER_RC", None)
    subprocess.run(
        ["bash", str(SCRIPT), "--narrative", "probe", "--from-sid", "sid-x"],
        capture_output=True, text=True, env=env)
    invocations = log.read_text() if log.exists() else ""
    spawn_line = next(l for l in invocations.splitlines() if "/manager-resume" in l)
    assert "--dangerously-skip-permissions" in spawn_line, spawn_line
    assert spawn_line.startswith("ENV_SKIP=unset"), spawn_line
