---
description: Reboot manager state after an in-place /clear (second half of /manager-recycle)
argument-hint: <handoff_id>
---

# /manager-reboot

You are the SAME manager process that just ran `/manager-recycle`: the conversation was `/clear`ed, but this is the same tab, same CLI process, same MCP server — and the four Monitor tasks are still running. This command re-registers the session and reloads state. It is typed automatically by the recycle sender; users normally never invoke it by hand.

Do the following in order:

1. Parse `$ARGUMENTS` for the `handoff_id`. If missing or empty, tell the user "ERROR: /manager-reboot requires a handoff_id" and stop.
2. Read `~/.claude/dockwright/handoffs/<handoff_id>.json`. If the file is missing, tell the user "ERROR: no handoff with id <handoff_id>" and stop. If `consumed_at` is non-null, tell the user "ERROR: handoff <handoff_id> already consumed at <consumed_at>" and stop.
3. Resolve `<your sid>` — `/clear` rotated it. Run `echo $CLAUDE_CODE_SESSION_ID`: the CLI re-stamps the env per child process, so this returns the NEW sid. Do not reuse the handoff's `from_sid`; do not invent a sid.
4. Call the `dockwright` MCP tool `become_manager` with `claude_sid=<your sid>`, `domain=<the handoff's domain>`, `name=<the handoff's manager_name>` — plain `become_manager`, NEVER `become_manager_with_takeover`. Takeover resolves the predecessor record's window and closes it; after an in-place clear the "predecessor" record IS this tab, so takeover would close the live manager. There is no predecessor process to kill and no tab to close. `become_manager` prunes the old-sid same-pid ghost record itself and keeps the name, so event buckets (`done/<name>/`, `questions/<name>/`, `turn-ends/<name>/`) and workers' `parent_manager_name` pointers stay valid. **First-run fallback (expected once per pre-existing manager):** if `become_manager` rejects the `name` argument, this manager's MCP server booted before the name param shipped — and `/clear` never restarts the MCP server, so no in-place recycle can fix that. Recover by calling `spawn_replacement_manager(handoff_id=<the same handoff_id>)`: the windowed flow takes over in a fresh tab whose MCP server has current code (every later `/manager-recycle` then works in-place). The takeover closes this tab as the predecessor; if it lingers (rare, post-/clear record drift), tell the user to close it manually. On this fallback path the recovered manager may register suffixed (e.g. `<name>-2`, because this tab's post-clear record still holds the original name) and opens in a new window — workers and event buckets re-bind on the next clean recycle.
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
7. Load the manager agent definition from `~/.claude/agents/manager.md`. **This file is larger than a single `Read` can return (~80 KB / 600+ lines, over the ~25k-token single-Read cap), so one `Read` silently truncates it — the later sections just never arrive, with no error.** You MUST page it to EOF before acting: run `wc -l ~/.claude/agents/manager.md` to get the total line count N, then `Read` it in windows of ≤200 lines (`offset=1`, then `offset=201`, then `offset=401`, … each with `limit=200`) until a window reaches line N. **Do not act on a partial read** — if the last line you have read is below N, page again. Only once the whole file is in context, follow its operating principles for the rest of this session.
8. Load recent manager memory + the manager notebook for your domain. Run `bash -c 'TODAY=$(date +%s); for f in ~/.claude/dockwright/manager-memory/<domain>/*.md; do [ -e "$f" ] || continue; mtime=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f"); age=$(( (TODAY - mtime) / 86400 )); [ "$age" -le 7 ] && echo "$mtime $f"; done | sort -rn | head -5 | while read -r _ f; do echo "MEMORY $f"; done; NB=~/.claude/dockwright/notebook/<domain>.md; if [ -s "$NB" ]; then SZ=$(wc -c < "$NB"); echo "NOTEBOOK $NB ($SZ bytes)"; [ "$SZ" -le 4096 ] || echo "NOTEBOOK_WARN [notebook >4KB ($SZ bytes) — archive resolved entries to notebook/archive/]"; fi' 2>/dev/null` — substitute `<domain>`. The command prints **paths only** — `Read` each printed `MEMORY <path>` and the `NOTEBOOK <path>` with the Read tool (Read paginates, so nothing is dropped). It used to `cat` the files inline, but 5 memory distills + the notebook can total ~35 KB and overflow the Bash inline-output cap, leaving only a ~2 KB preview in context — silently dropping memory and notebook content (this is why a boot could falsely report the notebook empty). The memory part loads the newest 5 files by mtime among those from the last 7 days (boot-cost cap); your own just-prepared distill is the newest file, so the cap never drops it. Fold these into your working context as predecessor context. Skip silently if empty. The notebook part prints a pointer to your domain's planned/conditional-work agenda (see `~/.claude/agents/manager.md` § Manager notebook): if it printed, count its open entries (`## [ ]` headers), evaluate which look ripe (date conditions by inspection; others via the entry's named `check:` command — one cheap call each), and add "N planned entries, M look ripe" to the reboot brief, flagging any entry past its `review-by` for triage. If the size warning printed, archive resolved entries per the agent file. If the notebook is absent or empty, skip silently.
9. **Monitor census — do NOT blindly re-arm.** The four Monitor loops survive `/clear`; re-arming all four would DUPLICATE every event ping. Census which kinds are alive and owned by THIS session, then re-arm only missing ones. Run, substituting `<your sid>`:

```bash
SID="<your sid>"
CLI=$(python3 -c "import json, sys; from pathlib import Path; print(json.loads((Path.home() / '.claude/dockwright/active' / (sys.argv[1] + '.json')).read_text())['pid'])" "$SID")
for kind in questions turn-ends done stale; do
  alive=""
  for pid in $(pgrep -f "(dockwright|orchestrator) monitor $kind" 2>/dev/null); do
    a="$pid"
    while [ -n "$a" ] && [ "$a" != "1" ]; do
      if [ "$a" = "$CLI" ]; then alive="$pid"; break; fi
      a=$(ps -o ppid= -p "$a" 2>/dev/null | tr -d ' ')
    done
    [ -n "$alive" ] && break
  done
  if [ -n "$alive" ]; then echo "$kind: alive"; else echo "$kind: MISSING"; fi
done
```

(`CLI` is this session's Claude CLI pid from the active record `become_manager` just wrote — the ancestor walk keeps the census multi-manager-safe: a peer manager's monitors descend from a different CLI pid. The `$kind` variable indirection keeps this script's own argv from matching `pgrep -f`. The census is a process census on purpose: TaskList does not enumerate Monitors, and pre-clear task ids are unknown post-clear.) For each kind reported `MISSING`, arm that Monitor exactly as `/manager` step 7 specifies: `while true; do dockwright monitor questions; sleep 2; done` / `while true; do dockwright monitor turn-ends; sleep 5; done` / `while true; do dockwright monitor done; sleep 2; done` / `while true; do dockwright monitor stale; sleep 60; done`. Normally all four report alive and this step arms nothing. If a kind ever double-fires later (census false-miss + re-arm), `kill <pid>` the newer duplicate loop process — TaskStop cannot target it.
10. Do NOT repaint the tab, do NOT close any window, do NOT spawn anything — the window is untouched by design (these are the deltas vs `/manager-resume` steps 4b/4c; the tab still carries your name + colors).
11. Surface a reboot brief: "Recycled in place (handoff `<first 8 chars>`). N workers in flight, M pending questions. Loaded K memory files (newest 5 within last 7 days)." plus the first paragraph of the handoff's `narrative_summary`, verbatim, plus the notebook line from step 8 if any. From this point on you are the active manager — resume orchestration.
