---
description: Reboot manager state after an in-place /clear (second half of /manager-recycle)
argument-hint: <handoff_id>
---

# /manager-reboot

You are the SAME manager process that just ran `/manager-recycle`: the conversation was `/clear`ed, but this is the same tab, same CLI process, same MCP server. The four lane monitors keep running across the clear: `/clear` unregisters the active record before the new session re-registers it, and a lane whose scan lands in that window waits the gap out under its own unresolved grace rather than ending. Step 9 re-invokes the arming skill anyway — the call is idempotent, so an already-armed lane is skipped. This command re-registers the session and reloads state. It is typed automatically by the recycle sender; users normally never invoke it by hand.

Do the following in order:

1. Parse `$ARGUMENTS` for the `handoff_id`. If missing or empty, tell the user "ERROR: /manager-reboot requires a handoff_id" and stop.
2. Read `~/.claude/dockwright/handoffs/<handoff_id>.json`. If the file is missing, tell the user "ERROR: no handoff with id <handoff_id>" and stop. If `consumed_at` is non-null, tell the user "ERROR: handoff <handoff_id> already consumed at <consumed_at>" and stop.
3. Resolve `<your sid>` — `/clear` rotated it. Run `printenv CLAUDE_CODE_SESSION_ID`: the CLI re-stamps the env per child process, so this returns the NEW sid. Do not reuse the handoff's `from_sid`; do not invent a sid.
4. Call the `dockwright` MCP tool `become_manager` with `claude_sid=<your sid>`, `domain=<the handoff's domain>`, `name=<the handoff's manager_name>` — plain `become_manager`, NEVER `become_manager_with_takeover`. Takeover resolves the predecessor record's window and closes it; after an in-place clear the "predecessor" record IS this tab, so takeover would close the live manager. There is no predecessor process to kill and no tab to close. `become_manager` prunes the old-sid same-pid ghost record itself and keeps the name, so event buckets (`done/<name>/`, `questions/<name>/`, `turn-ends/<name>/`) and workers' `parent_manager_name` pointers stay valid. `become_manager` also runs preflight cleanup server-side and idempotently re-affirms your tab paint (same name + colors — a no-op in effect, since `/clear` left the tab untouched); surface the returned `preflight` line to the user if non-empty (older orchestrator builds may omit the field; then skip silently). **First-run fallback (expected once per pre-existing manager):** if `become_manager` rejects the `name` argument, this manager's MCP server booted before the name param shipped — and `/clear` never restarts the MCP server, so no in-place recycle can fix that. Recover by calling `spawn_replacement_manager(handoff_id=<the same handoff_id>)`: the windowed flow takes over in a fresh tab whose MCP server has current code (every later `/manager-recycle` then works in-place). The takeover closes this tab as the predecessor; if it lingers (rare, post-/clear record drift), tell the user to close it manually. On this fallback path the recovered manager may register suffixed (e.g. `<name>-2`, because this tab's post-clear record still holds the original name) and opens in a new window — workers and event buckets re-bind on the next clean recycle.
5. Mark the handoff consumed (so a later `/manager-resume` can't double-consume it) and append the in-place trigger to the trigger log — runtime-state mutation via one Bash call, substituting `<handoff_id>` and `<your sid>`:

```bash
python3 - "<handoff_id>" "<your sid>" <<'EOF'
import json, sys, time
from pathlib import Path
root = Path.home() / ".claude/dockwright"
p = root / "handoffs" / f"{sys.argv[1]}.json"
d = json.loads(p.read_text())
now = time.time()
d["consumed_at"] = now
d["to_sid"] = sys.argv[2]
tmp = p.with_suffix(".json.tmp")
tmp.write_text(json.dumps(d, indent=2))
tmp.rename(p)
with (root / "manager-triggers.jsonl").open("a") as f:
    f.write(json.dumps({
        "ts": now,
        "from_sid": d.get("from_sid"),
        "to_sid": sys.argv[2],
        "handoff_id": sys.argv[1],
        "trigger_reason": d.get("trigger_reason"),
        "narrative_excerpt": (d.get("narrative_summary") or "")[:200],
    }) + "\n")
print(f"consumed handoff {sys.argv[1][:8]}, trigger logged")
EOF
```

(The entry shape mirrors what the windowed takeover lane appends, so `trigger_reason="in-place"` rows are countable alongside `manual`/`mcp-refresh` ones.)

6. Call `attach_existing` to enumerate workers and orphan questions — they carried across the clear untouched.
7. If your system prompt already contains the line `<!-- dockwright-role: Manager launch=manager -->`, your manager definition is loaded: skip this Read. Otherwise load the manager definition from `~/.claude/roles/Manager/role.manager.md` when that file exists, else from `~/.claude/agents/manager.md`. **This file (~46 KB / 388 lines shipped; an operator overlay adds to that) can with an overlay exceed the ~25k-token single-Read cap, so one `Read` silently truncates it — the later sections just never arrive, with no error.** You MUST page it to EOF before acting: the total line count N is the `AGENT_LINES <N>` line printed by `dockwright boot-brief` (step 8 below). `Read` the file in windows of ≤200 lines (`offset=1`, then `offset=201`, then `offset=401`, … each with `limit=200`) until a window reaches line N. **Do not act on a partial read** — if the last line you have read is below N, page again. Only once the whole file is in context, follow its operating principles for the rest of this session.
8. Load recent manager memory + the manager notebook for your domain. Run `dockwright boot-brief --domain <domain>` (substitute `<domain>`). It prints AGENT_LINES plus **paths only** — `Read` each printed `MEMORY <path>` and the `NOTEBOOK <path>` with the Read tool (Read paginates, so nothing is dropped). It prints pointers rather than file contents because the memory distills + the notebook can overflow the Bash inline-output cap, leaving only a ~2 KB preview in context — silently dropping memory and notebook content (this is why an old inline-`cat` boot could falsely report the notebook empty). The `MEMORY` pointers are the newest 3 files by mtime among those from the last 7 days (boot-cost cap); your own just-prepared distill is the newest file, so the cap never drops it. Fold these into your working context as predecessor context. Skip silently if none printed. The `NOTEBOOK` pointer is your domain's current-state agenda (see `~/.claude/agents/manager.md` § Manager notebook): if it printed, count its unresolved entries — every `## ` header not marked `## [x]`, the same selector the agent file's § Manager notebook names, because a ruling entry carries no checkbox and `## [ ]` silently misses it, evaluate which look ripe (date conditions by inspection; others via the entry's named `check:` command — one cheap call each), and add "N unarchived entries, M look ripe" to the reboot brief, flagging any entry past its `review-by` for triage. ⛔ Naming a specific entry in the brief is a RELAY, and the agent file's § Manager notebook gate binds it: run that entry's `check:` first. A brief that only COUNTS entries asserts nothing and needs no check. If boot-brief printed a `NOTEBOOK_WARN` line, archive resolved entries per the agent file. If no `NOTEBOOK` line printed, skip silently.
9. Arm the four lane monitors. Plugin monitors survive `/clear`; the lane loop waits out the re-registration window itself. Invoke `Skill(dockwright:arm-lanes)`. The dockwright plugin then starts the four lane monitors (`dockwright monitor-loop questions|done|turn-ends|stale`); a lane already armed in this CLI process is skipped, so the call is idempotent. Wait 10 s (`sleep 10`) before the first `dockwright lanes` — arming lands about 5 s after the skill call and a lane needs one more scan to heartbeat, so an immediate read shows `NEVER-ARMED` or `ARMED-UNRESOLVED` rows on a healthy install; if any row reads `NEVER-ARMED` or `ARMED-UNRESOLVED`, run `dockwright lanes` once more after another 10 s before you follow its recovery line. For any row that is still not `OK`, do what its recovery line says; the fallback it may name is a degraded mode — say so in your brief.

10. Do NOT issue any manual tab-paint or window-close bash, do NOT spawn anything — the window is untouched by design (become_manager's server-side paint in step 4 just re-affirms the same name + colors; contrast `/manager-resume`'s takeover path, which repaints a fresh tab and closes the predecessor pane). The tab still carries your name + colors.
11. Surface a reboot brief: "Recycled in place (handoff `<first 8 chars>`). N workers in flight, M pending questions. Loaded K memory files (newest 3 within last 7 days)." plus the first paragraph of the handoff's `narrative_summary`, verbatim, plus the notebook line from step 8 if any. From this point on you are the active manager — resume orchestration.
