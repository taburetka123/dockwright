---
name: dockwright-todo
description: Save a todo for later processing. Accepts text after the slash command (e.g. "/dockwright-todo rebase branch onto main") and writes it to ~/.claude/todos/ for later pickup via /dockwright-dotodo. Triggers on "save todo", "remember to", "add todo", "/dockwright-todo <text>".
user-invocable: true
disable-model-invocation: false
---

# Save Todo

Persist the user's todo text to `~/.claude/todos/` as a timestamped markdown file. Later, `/dockwright-dotodo` will pop the oldest entry and process it.

## Storage contract

- Directory: `~/.claude/todos/` (cross-project, global).
- Filename: `<unix-ts>-<8hex>.md` — lexicographically sortable = chronologically sortable.
- File body: the raw todo text, UTF-8, no frontmatter.

## Steps

1. Read `$ARGUMENTS` — the text after the slash command. If empty, print `Usage: /dockwright-todo <text>` and stop.

2. Write the file using a single bash command:
   ```bash
   mkdir -p ~/.claude/todos
   filename="$(python3 -c "import time, secrets; print(f'{int(time.time())}-{secrets.token_hex(4)}.md')")"
   printf '%s\n' "$ARGUMENTS" > "$HOME/.claude/todos/$filename"
   echo "Saved todo (~/.claude/todos/$filename)"
   ```

3. Confirm in chat: `Saved todo (~/.claude/todos/<filename>): <first 60 chars of text>…`. Keep it to one line.

That's it — no other logic, no follow-up tasks.
