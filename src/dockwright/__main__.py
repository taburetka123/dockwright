import sys

USAGE = """\
Usage: dockwright <subcommand>

Subcommands:
  manager               Launch (or reattach to) the manager tmux session
  doctor                Verify install wiring (hooks, MCP, venv)
  init                  Write a starter dockwright.toml
  model                 Model tokens: resolve <@family[variant]|concrete-id> | manager
  pipeline              Pipeline schemas and works: validate <schema-name-or-path> [--cwd DIR] | status <KEY> [--json] | active-paths [--json]
  schema                Show a pipeline schema: show <schema-name-or-path> (nodes, children, subagents, edges)
  notebook-migrate      Notebook to note topics: <domain> [--decisions MARKER] [--dry-run]
  notebook              Notebook actualization mark: mark --sid S [--domain D] [--not-actualized REASON] | check --sid S [--domain D]
  board                 Board: [inbox] [--domain D] | topics [--domain D] [--kind K] [--status open|closed] [--json] | topic <KEY> | --serve [--port N]
  tech-debt             Tech-debt entries: [--tag T]... [--domain D] [--new] [--check] [--json] | --untagged [--domain D] | --seen FILE | --archive FILE | tags
  topic                 Write to a topic: message <KEY> [--kind message|answer|priority] [--text T | --file F] [--question QID] [--priority P0..P3] | message <KEY> --kind verdict --round N --file F | new --domain D (--text T | --file F) | record <KEY> <phase.name> | close <KEY> --node N --text T [--checkpoint P.N=F]... [--output P.N=F]... [--expect P.N=W]...
  event                 Event subscriptions: subscribe --task-key K --node N --source S --match k=v... [--kinds a,b] [--after X] [--deadline TS] [--skip-own ID] [--handler CMD] | unsubscribe <sub_id> | --task-key K --node N | list [K] | emit --file F | gate <rule>
  chat-archive          Chat archive: pass | read --after X [--before Y] [--conversation ID] [--thread TS] [--no-bots] | search QUERY [--after X] | conversations | status | reindex | sync | loop
  monitor               One-shot fleet scans: questions|done|turn-ends|stale [manager-name]
  monitor-loop          Plugin-monitor lane loop: monitor-loop questions|done|turn-ends|stale
  lanes                 Report whether this manager's monitor lanes are still delivering
  daemon                Fleet daemon, two launchd jobs ([daemon] in dockwright.toml): tick | observe | sync | status | schedules
  revive                Reopen a manager window per manager lost to the last host reboot (--resume: on its own transcript)
  pause                 Quiet the fleet for a vacation: stop files, launchd jobs, workers archived as fleet-pause, managers closed, tmux killed (--dry-run)
  resume                Bring the paused fleet back: managers on their own transcripts, then the daemon (--dry-run, --no-attach)
  sweep                 Prune stale orchestrator state
  spend-report          Fleet token report, or per-session time+cost report (spend-report <worker|sid>)
  spend-cost            Price a spend report
  spend-fleet           Per-call fleet spend over a date window: calls, sessions and a cross-checked summary (--out DIR)
  usage-cap             Show each operator usage cap (usage-caps.toml in the state root) and whether it is breached (--paused: exit 0 while worker starts are refused)
  account-hold          Put a pool account on hold (spawns and flips skip it; running sessions finish on it) or release it: <name> [--reason TEXT] | --release <name> | (no args: list)
  distill               Distill a manager session into a memory journal
  boot-brief            Print manager boot pointers (agent line count, memory/notebook paths)
  selffix               Enable/disable the SessionEnd retrospective hook
  gardener              Enable/disable the gardener loops (macOS launchd)
  ensure-worker-home    Create + pre-trust the default worker home
  ensure-manager-home   Create + pre-trust the manager home every manager launch pins
  manager-rc-args       Remote Control args a manager launch carries: [--account NAME]
  manager-launch-args   Every flag a manager launch carries (Remote Control, skip-perms, effort, model): [--account NAME]
  accounts-sync         Reconcile pool-account config-dir farms (symlinks + MCP refresh)
  write-registry-snapshot
                        Refresh account-registry.json for standalone consumers
  compose               Render agent files from cores + overlay
  role-args             Print the flags that launch a session in a role
  roles                 Role catalog: catalog [--check] [--write-baseline] [--report]
  launch                Exec `claude` with --agents-file <path> expanded into --agents <json> (no shell in between)
  render                Render command/preset templates
  finalize-presets      Render permission presets
  install-hooks         Install Claude/Codex hook wiring
  translate-codex       Rebuild ~/.codex (AGENTS.md, skills, agents) from ~/.claude; --check to diff
  render-agents-md      Concatenate markdown sources into one AGENTS.md-shaped file (--out FILE SRC...)
  install-codex-skills  DEPRECATED (removed next release) — writes command wrappers only; setup.sh now runs translate-codex
  clean-homebrew        Remove legacy Homebrew artifacts
  migrate-state         Migrate legacy state roots
  assign-to-manager     Promote this session into a managed worker
  uninstall             Remove dockwright from this machine

Internal (hook/MCP plumbing — wired by setup.sh, not typed by hand):
  mcp-server            MCP stdio server (registered via `claude mcp add`)
  session-start | user-prompt-submit | stop | stop-failure | session-end | subagent-start | permission-request | post-tool-use
                        Claude Code lifecycle hooks
  reply-guard           PreToolUse guard of a topic agent's reply session (wired by reply-settings.json)
  agent-guard           PreToolUse guard of a pipeline node: denies an undeclared Agent call and any Workflow"""

def main() -> None:
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd in ("--help", "-h", "help"):
        print(USAGE)
        sys.exit(0)
    if cmd == "mcp-server":
        from .mcp_server import main as mcp_main
        mcp_main()
    elif cmd == "session-start":
        from .hooks import session_start
        session_start()
    elif cmd == "user-prompt-submit":
        from .hooks import user_prompt_submit
        user_prompt_submit()
    elif cmd == "stop":
        from .hooks import stop_hook
        stop_hook()
    elif cmd == "stop-failure":
        from .hooks import stop_failure
        stop_failure()
    elif cmd == "session-end":
        from .hooks import session_end
        session_end()
    elif cmd == "permission-request":
        from .hooks import permission_request
        permission_request()
    elif cmd == "post-tool-use":
        from .hooks import post_tool_use
        post_tool_use()
    elif cmd == "assign-to-manager":
        from .promote import assign_to_manager_cli
        assign_to_manager_cli()
    elif cmd == "monitor":
        from .monitor import main as monitor_main
        monitor_main(sys.argv[2:])
    elif cmd == "monitor-loop":
        from .monitor_loop import main as monitor_loop_main
        sys.exit(monitor_loop_main(sys.argv[2:]))
    elif cmd == "lanes":
        from .lanes import main as lanes_main
        sys.exit(lanes_main(sys.argv[2:]))
    elif cmd == "install-codex-skills":
        from .codex_skills import main as codex_skills_main
        sys.exit(codex_skills_main(sys.argv[2:]))
    elif cmd == "translate-codex":
        from .codex_translate import main as translate_codex_main
        sys.exit(translate_codex_main(sys.argv[2:]))
    elif cmd == "render-agents-md":
        from .codex_translate import render_main as render_agents_md_main
        sys.exit(render_agents_md_main(sys.argv[2:]))
    elif cmd == "revive":
        from .revive import main as revive_main
        sys.exit(revive_main(sys.argv[2:]))
    elif cmd == "pause":
        from .pause import main as pause_main
        sys.exit(pause_main(["pause", *sys.argv[2:]]))
    elif cmd == "resume":
        from .pause import main as pause_main
        sys.exit(pause_main(["resume", *sys.argv[2:]]))
    elif cmd == "sweep":
        from .sweep import main as sweep_main
        sys.exit(sweep_main(sys.argv[2:]))
    elif cmd == "spend-report":
        from .spend_report import main as spend_report_main
        sys.exit(spend_report_main(sys.argv[2:]))
    elif cmd == "spend-cost":
        from .spend_cost import main as spend_cost_main
        sys.exit(spend_cost_main(sys.argv[2:]))
    elif cmd == "spend-fleet":
        from .spend_fleet import main as spend_fleet_main
        sys.exit(spend_fleet_main(sys.argv[2:]))
    elif cmd == "distill":
        from .distill import main as distill_main
        sys.exit(distill_main(sys.argv[2:]))
    elif cmd == "install-hooks":
        from .env_install import main as install_hooks_main
        sys.exit(install_hooks_main(sys.argv[2:]))
    elif cmd == "clean-homebrew":
        from .homebrew_cleanup import main as clean_homebrew_main
        sys.exit(clean_homebrew_main(sys.argv[2:]))
    elif cmd == "doctor":
        from .doctor import main as doctor_main
        sys.exit(doctor_main(sys.argv[2:]))
    elif cmd == "init":
        from .init_config import main as init_main
        sys.exit(init_main(sys.argv[2:]))
    elif cmd == "compose":
        from .compose import main as compose_main
        sys.exit(compose_main(sys.argv[2:]))
    elif cmd == "roles" and len(sys.argv) > 2 and sys.argv[2] == "catalog":
        from .catalog import main as catalog_main
        sys.exit(catalog_main(sys.argv[3:]))
    elif cmd == "launch":
        from .launch import main as launch_main
        sys.exit(launch_main(sys.argv[2:]))
    elif cmd == "subagent-start":
        from .hooks import subagent_start
        subagent_start()
    elif cmd == "role-args":
        from .roles import main as role_args_main
        sys.exit(role_args_main(sys.argv[2:]))
    elif cmd == "render":
        from .render import main as render_main
        sys.exit(render_main(sys.argv[2:]))
    elif cmd == "uninstall":
        from .uninstall import main as uninstall_main
        sys.exit(uninstall_main(sys.argv[2:]))
    elif cmd == "migrate-state":
        from .migrate import main as migrate_main
        sys.exit(migrate_main(sys.argv[2:]))
    elif cmd == "manager":
        from .manager_launch import main as manager_main
        sys.exit(manager_main(sys.argv[2:]))
    elif cmd == "manager-rc-args":
        from .manager_launch import manager_rc_args_main
        sys.exit(manager_rc_args_main(sys.argv[2:]))
    elif cmd == "manager-launch-args":
        from .manager_launch import manager_launch_args_main
        sys.exit(manager_launch_args_main(sys.argv[2:]))
    elif cmd == "ensure-worker-home":
        from .ensure_worker_home import main as ewh_main
        sys.exit(ewh_main(sys.argv[2:]))
    elif cmd == "ensure-manager-home":
        from .ensure_manager_home import main as emh_main
        sys.exit(emh_main(sys.argv[2:]))
    elif cmd == "accounts-sync":
        from .accounts_sync import main as accounts_sync_main
        sys.exit(accounts_sync_main(sys.argv[2:]))
    elif cmd == "write-registry-snapshot":
        from .spawner import write_registry_snapshot
        write_registry_snapshot()
        sys.exit(0)
    elif cmd == "selffix":
        from .pipeline_wiring import selffix_main
        sys.exit(selffix_main(sys.argv[2:]))
    elif cmd == "gardener":
        from .pipeline_wiring import gardener_main
        sys.exit(gardener_main(sys.argv[2:]))
    elif cmd == "finalize-presets":
        from .presets import main as finalize_presets_main
        sys.exit(finalize_presets_main(sys.argv[2:]))
    elif cmd == "reply-guard":
        from .reply_guard import main as reply_guard_main
        sys.exit(reply_guard_main(sys.argv[2:]))
    elif cmd == "agent-guard":
        from .agent_guard import main as agent_guard_main
        sys.exit(agent_guard_main(sys.argv[2:]))
    elif cmd == "boot-brief":
        from .boot_brief import main as boot_brief_main
        sys.exit(boot_brief_main(sys.argv[2:]))
    elif cmd == "model":
        from .models import model_main
        sys.exit(model_main(sys.argv[2:]))
    elif cmd == "pipeline":
        from .pipeline import main as pipeline_main
        sys.exit(pipeline_main(sys.argv[2:]))
    elif cmd == "schema":
        from .pipeline import schema_main
        sys.exit(schema_main(sys.argv[2:]))
    elif cmd == "notebook-migrate":
        from .notebook_migrate import main as notebook_migrate_main
        sys.exit(notebook_migrate_main(sys.argv[2:]))
    elif cmd == "notebook":
        from .notebook_mark import main as notebook_main
        sys.exit(notebook_main(sys.argv[2:]))
    elif cmd == "tech-debt":
        from .tech_debt import main as tech_debt_main
        sys.exit(tech_debt_main(sys.argv[2:]))
    elif cmd == "board":
        from .board import main as board_main
        sys.exit(board_main(sys.argv[2:]))
    elif cmd == "topic":
        from .topic_message import main as topic_main
        sys.exit(topic_main(sys.argv[2:]))
    elif cmd == "event":
        from .events import main as event_main
        sys.exit(event_main(sys.argv[2:]))
    elif cmd == "daemon":
        from .daemon import main as daemon_main
        sys.exit(daemon_main(sys.argv[2:]))
    elif cmd == "chat-archive":
        from .chat_archive import main as chat_main
        sys.exit(chat_main(sys.argv[2:]))
    elif cmd == "usage-cap":
        from .usage_cap import main as usage_cap_main
        sys.exit(usage_cap_main(sys.argv[2:]))
    elif cmd == "account-hold":
        from .account_hold import main as account_hold_main
        sys.exit(account_hold_main(sys.argv[2:]))
    else:
        print(f"Unknown subcommand: {cmd}", file=sys.stderr)
        print("Run `dockwright --help` for the full list.", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
