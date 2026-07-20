"""N-1 regression guard: no platform-split stat probe in the deployed payload.

The old boot memory-loader used an inline `bash -c` idiom whose mtime probe
`stat -f %m "$f" 2>/dev/null || stat -c %Y "$f"` was GNU-poisoned: `stat -f
<file>` prints an fs-info block to STDOUT before exiting 1, `$(a || b)`
concatenates both stdouts, and the age arithmetic aborts — every Linux manager
boot then reported "no memory" (E2E rc.2 N-1). rc.2 fixed it to the portable
`date -r "$f" +%s` form.

The zero-touch-headless migration (E2E F-2) then retired the inline bash entirely:
the loader is now `dockwright boot-brief --domain <d>`, which reads mtimes via
Python `Path.stat().st_mtime` — inherently portable, no shell `stat`/`date`. The
newest-5 / 7-day-window / notebook-warn selection is covered directly by
tests/test_boot_brief.py, and tests/test_manager_boot_docs.py pins that the docs
carry no inline `date -r` / stat probe. So the per-snippet extraction tests are
gone (there is no snippet to extract); this broad class-guard survives — it keeps
any GNU-poisoned `stat -f` / macOS-breaking `stat -c` platform-split probe from
being reintroduced ANYWHERE under deploy/.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"


def test_no_platform_split_stat_probes_in_deploy():
    # Guard the whole probe class: `stat -f` is GNU-poisoned, a bare `stat -c`
    # silently breaks macOS. Portable forms: the boot-brief CLI (Python stat) or,
    # for any shell script that still needs an mtime, `date -r <path> +%s`.
    offenders = []
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue
        if "stat -f" in text or "stat -c" in text:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        f"platform-split stat probes found (GNU `stat -f`/`stat -c` breaks "
        f"across macOS/Linux; use the boot-brief CLI or the portable "
        f"`date -r <path> +%s` form instead): {offenders}")
