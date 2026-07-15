---
name: dockwright-dotodo
description: List pending todos from ~/.claude/todos/, ask which one to process, then process it as the next task. Triggers on "process next todo", "do next todo", "pick todo", "/dockwright-dotodo".
user-invocable: true
disable-model-invocation: false
---

# Process Next Todo

List pending todo files in `~/.claude/todos/` (saved via `/dockwright-todo`), ask the user which to process, then present its content and proceed as if the user had just typed that content.

## Storage contract

- Source: `~/.claude/todos/*.md` — sorted lexicographically (filenames are `<unix-ts>-<hex>.md`, so this is chronological).
- Consumed: MOVE the selected file to `~/.claude/todos/done/<same-filename>` to preserve the audit trail. Do NOT delete.

## Steps

1. List pending todos with a 1-line preview each:
   ```bash
   mkdir -p ~/.claude/todos ~/.claude/todos/done
   i=1
   for f in $(find ~/.claude/todos -maxdepth 1 -name '*.md' 2>/dev/null | sort); do
     preview=$(grep -m1 . "$f" | cut -c1-80)
     echo "$i. $preview  ($(basename "$f"))"
     i=$((i+1))
   done
   ```
   If the loop produces no output, print `no todos pending` and stop.

2. Ask the user which todo to process using `AskUserQuestion`:
   - header: `Pick todo`
   - One option per todo. Label = preview truncated to ~45 chars. Description = the full preview + filename.
   - If there are more than 4 todos, present the first 3 as direct options + let the user type the index via the auto-injected "Other" option.
   - If 4 or fewer todos, present all of them directly.

3. Resolve the user's selection to a filename. If they answered via "Other" with an index like `5`, map it back to the Nth entry from the listing in step 1.

4. Move the selected file to `~/.claude/todos/done/`:
   ```bash
   mv "$selected" "$HOME/.claude/todos/done/$(basename "$selected")"
   ```

5. Print a one-line context note: `Processing todo from <filename>:` followed by the file content.

6. Treat the file content as the user's next task. Apply normal skill-invocation rules — if the content itself references another skill or matches a skill trigger, invoke that skill before responding.
