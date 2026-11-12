import argparse
import asyncio
import io
import json
import os
import sys
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--ask", action="store_true")
    parser.add_argument("--resume", default=None)
    args = parser.parse_args()
    from dockwright import hooks
    from dockwright.mcp_server import ask_manager_impl, register_self_impl
    sid = args.resume or f"fake-{uuid.uuid4().hex[:8]}"
    name = os.environ["CLAUDE_WORKER_NAME"]
    register_self_impl(
        claude_sid=sid, agent="worker", name=name, cwd=os.getcwd(), iterm_sid="", pid=os.getpid(),
        parent_manager_name=os.environ.get("CLAUDE_PARENT_MANAGER") or None,
        parent_sid=os.environ.get("CLAUDE_PARENT_SID") or None,
        kind=os.environ.get("CLAUDE_WORKER_KIND") or None,
        provider=os.environ.get("CLAUDE_WORKER_PROVIDER") or None,
    )
    hooks._claim_pending_assignment(sid, name)
    with open(args.brief) as fh:
        brief = fh.read()
    answer = ""
    if args.ask:
        answer = asyncio.run(ask_manager_impl(sid, "fake ping", poll_interval=0.1, timeout_sec=30,
                                              tool_name="ask_parent", sid_param="child_sid"))
    echoed = "".join(f"brief={line}\n" for line in brief.splitlines() if line.strip())
    with open(args.report, "w") as fh:
        fh.write(f"REPORT sid={sid}\n{echoed}answer={answer}\n")
    sys.stdin = io.StringIO(json.dumps({"session_id": sid, "reason": "exit"}))
    hooks.session_end()
    return 0


if __name__ == "__main__":
    sys.exit(main())
