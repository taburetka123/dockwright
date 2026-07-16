"""CLI dispatcher: `dockwright <subcommand>`."""
import sys

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: dockwright <subcommand>", file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
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
    elif cmd == "session-end":
        from .hooks import session_end
        session_end()
    elif cmd == "assign-to-manager":
        from .promote import assign_to_manager_cli
        assign_to_manager_cli()
    elif cmd == "monitor":
        from .monitor import main as monitor_main
        monitor_main(sys.argv[2:])
    elif cmd == "install-codex-skills":
        from .codex_skills import main as codex_skills_main
        sys.exit(codex_skills_main(sys.argv[2:]))
    elif cmd == "sweep":
        from .sweep import main as sweep_main
        sys.exit(sweep_main(sys.argv[2:]))
    elif cmd == "spend-report":
        from .spend_report import main as spend_report_main
        sys.exit(spend_report_main(sys.argv[2:]))
    elif cmd == "spend-cost":
        from .spend_cost import main as spend_cost_main
        sys.exit(spend_cost_main(sys.argv[2:]))
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
    elif cmd == "ensure-worker-home":
        from .ensure_worker_home import main as ewh_main
        sys.exit(ewh_main(sys.argv[2:]))
    elif cmd == "selffix":
        from .pipeline_wiring import selffix_main
        sys.exit(selffix_main(sys.argv[2:]))
    elif cmd == "gardener":
        from .pipeline_wiring import gardener_main
        sys.exit(gardener_main(sys.argv[2:]))
    elif cmd == "finalize-presets":
        from .presets import main as finalize_presets_main
        sys.exit(finalize_presets_main(sys.argv[2:]))
    else:
        print(f"Unknown subcommand: {cmd}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
