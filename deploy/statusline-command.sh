#!/bin/sh
input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // empty')
session_id=$(echo "$input" | jq -r '.session_id // empty')
branch=$(git -C "$cwd" --no-optional-locks symbolic-ref --short HEAD 2>/dev/null)
dir=$(basename "$cwd")

pending_dir="$HOME/.claude/dockwright/gardener/proposals/pending"
[ -d "$pending_dir" ] || pending_dir="$HOME/.claude/gardener/proposals/pending"
pending_proposals=$(find "$pending_dir" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
if [ "$pending_proposals" -gt 0 ] 2>/dev/null; then
  proposals=$(printf " \033[38;5;141m· 🌱 %s proposals\033[0m" "$pending_proposals")
else
  proposals=""
fi

todos_count=$(find "$HOME/.claude/todos" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
if [ "$todos_count" -gt 0 ] 2>/dev/null; then
  todos=$(printf " \033[38;5;82m· %s todos\033[0m" "$todos_count")
else
  todos=""
fi

rate_badge() {
  case "$2" in ''|*[!0-9]*) return 0;; esac
  if [ "$2" -lt 60 ]; then color=82
  elif [ "$2" -le 85 ]; then color=220
  else color=196; fi
  printf " \033[38;5;%sm· %s %s%%\033[0m" "$color" "$1" "$2"
}
five_hour_pct=$(echo "$input" | jq -r '(.rate_limits.five_hour.used_percentage // empty) | round' 2>/dev/null)
seven_day_pct=$(echo "$input" | jq -r '(.rate_limits.seven_day.used_percentage // empty) | round' 2>/dev/null)
ratelimits="$(rate_badge 5h "$five_hour_pct")$(rate_badge 7d "$seven_day_pct")"

ccd="${CLAUDE_CONFIG_DIR:-}"
if [ -z "$ccd" ] || [ "$ccd" = "$HOME/.claude" ]; then
  cfg_label="default"
else
  cfg_base=$(basename "$ccd")
  cfg_label="${cfg_base#.claude-}"
fi
cfg_badge=$(printf " \033[38;5;245m· cfg:%s\033[0m" "$cfg_label")

if echo "$input" | jq -e '.rate_limits != null' >/dev/null 2>&1; then
  usage_acct="${CLAUDE_ORCH_ACCOUNT:-}"
  if [ -z "$usage_acct" ]; then
    if [ "$cfg_label" = "default" ]; then usage_acct="a"; else usage_acct="$cfg_label"; fi
  fi
  case "$usage_acct" in
    ""|.*|*/*) : ;;
    *)
      usage_dir="$HOME/.claude/dockwright/usage"
      [ -d "$usage_dir" ] || { [ -d "$HOME/.claude/orchestrator/usage" ] && usage_dir="$HOME/.claude/orchestrator/usage"; }
      if mkdir -p "$usage_dir" 2>/dev/null; then
        usage_tmp="$usage_dir/.$usage_acct.json.tmp.$$"
        if echo "$input" | jq -c --argjson ts "$(date +%s)" '{
              five_hour_pct: (.rate_limits.five_hour.used_percentage // null),
              seven_day_pct: (.rate_limits.seven_day.used_percentage // null),
              five_hour_resets_at: (.rate_limits.five_hour.resets_at // null),
              seven_day_resets_at: (.rate_limits.seven_day.resets_at // null),
              ts: $ts
            }' > "$usage_tmp" 2>/dev/null; then
          mv -f "$usage_tmp" "$usage_dir/$usage_acct.json" 2>/dev/null || rm -f "$usage_tmp" 2>/dev/null
        else
          rm -f "$usage_tmp" 2>/dev/null
        fi
      fi
      ;;
  esac
fi

model_name=$(echo "$input" | jq -r '.model.display_name // .model.id // empty' 2>/dev/null)
effort_level=$(echo "$input" | jq -r '.effort.level // empty' 2>/dev/null)
model_effort=""
if [ -n "$model_name" ]; then
  if [ -n "$effort_level" ]; then
    model_effort=$(printf " \033[38;5;75m◆ %s · %s\033[0m" "$model_name" "$effort_level")
  else
    model_effort=$(printf " \033[38;5;75m◆ %s\033[0m" "$model_name")
  fi
fi

worker_label=""
mgr_identity=""
mgr_workers_row=""
active_dir="$HOME/.claude/dockwright/active"
[ -d "$active_dir" ] || active_dir="$HOME/.claude/orchestrator/active"
record=""
[ -n "$session_id" ] && [ -f "$active_dir/$session_id.json" ] && record="$active_dir/$session_id.json"

agent=""
[ -n "$record" ] && agent=$(jq -r '.agent // empty' "$record" 2>/dev/null)
[ -z "$agent" ] && agent="${CLAUDE_AGENT:-}"

if [ "$agent" = "manager" ]; then
  manager_name=""
  manager_domain=""
  if [ -n "$record" ]; then
    manager_name=$(jq -r '.name // empty' "$record" 2>/dev/null)
    manager_domain=$(jq -r '.domain // empty' "$record" 2>/dev/null)
  fi
  if [ -n "$manager_name" ]; then
    if [ -n "$manager_domain" ]; then
      mgr_identity=$(printf "\033[38;5;117m%s\033[0m \033[38;5;213m· %s\033[0m" "$manager_name" "$manager_domain")
    else
      mgr_identity=$(printf "\033[38;5;117m%s\033[0m" "$manager_name")
    fi
  fi

  idle=0
  processing=0
  if [ -d "$active_dir" ]; then
    counts=$(jq -r --arg mgr "$manager_name" \
      'select(.agent == "worker" and (.parent_manager_name == $mgr or .parent_manager_name == null) and .pid != null) | [.pid, .state, .claude_sid, (.transcript_path // "")] | @tsv' \
      "$active_dir"/*.json 2>/dev/null \
      | while IFS=$(printf '\t') read -r pid state sid tp; do
          [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || continue
          if [ "$state" = "idle" ] && [ -n "$tp" ] && [ -n "$sid" ]; then
            subagents_dir="$(dirname "$tp")/$sid/subagents"
            if [ -n "$(find "$subagents_dir" -name 'agent-*.jsonl' -newer "$tp" -mmin -15 2>/dev/null | head -n 1)" ]; then
              state="processing"
            fi
          fi
          echo "$state"
        done \
      | awk 'BEGIN{i=0;p=0} /^idle$/{i++} /^processing$/{p++} END{printf "%d %d", i, p}')
    set -- $counts
    idle=${1:-0}
    processing=${2:-0}
  fi
  mgr_workers_row=$(printf "\033[38;5;208m🤖 %di / %dp\033[0m" "$idle" "$processing")
elif [ "$agent" = "worker" ]; then
  funny_name=""
  task_name=""
  parent_name=""
  if [ -n "$record" ]; then
    funny_name=$(jq -r '.funny_name // empty' "$record" 2>/dev/null)
    task_name=$(jq -r '.name // empty' "$record" 2>/dev/null)
    parent_name=$(jq -r '.parent_manager_name // empty' "$record" 2>/dev/null)
  fi
  if [ -n "$funny_name" ]; then
    worker_label="$worker_label$(printf " \033[38;5;117m%s\033[0m" "$funny_name")"
  fi
  if [ -n "$task_name" ]; then
    worker_label="$worker_label$(printf " \033[38;5;208m· %s\033[0m" "$task_name")"
  fi
  if [ -n "$parent_name" ]; then
    worker_label="$worker_label$(printf " \033[38;5;213m⟵ %s\033[0m" "$parent_name")"
  fi
fi

if [ "$agent" = "manager" ]; then
  row1="$dir"
  [ -n "$branch" ] && row1="$row1  $branch"
  if [ -n "$mgr_identity" ]; then
    row1="$row1  $(printf '\033[38;5;213m🎯\033[0m') $mgr_identity"
  fi
  [ -n "$mgr_workers_row" ] && row1="$row1  $mgr_workers_row"
  row2="$ratelimits$cfg_badge$model_effort"
  row3="$proposals$todos"
  printf '%s\n' "$row1"
  printf '%s\n' "${row2# }"
  printf '%s' "${row3# }"
elif [ "$agent" = "worker" ]; then
  row1="$dir"
  [ -n "$branch" ] && row1="$row1  $branch"
  row2="${worker_label}${model_effort}"
  row3="$ratelimits$cfg_badge"
  row4="$proposals$todos"
  printf '%s\n' "$row1"
  printf '%s\n' "${row2# }"
  printf '%s\n' "${row3# }"
  printf '%s' "${row4# }"
elif [ -n "$branch" ]; then
  printf "%s  %s%s%s%s%s" "$dir" "$branch" "$proposals" "$todos" "$ratelimits" "$cfg_badge"
  [ -n "$model_effort" ] && printf '\n%s' "${model_effort# }"
else
  printf "%s%s%s%s%s" "$dir" "$proposals" "$todos" "$ratelimits" "$cfg_badge"
  [ -n "$model_effort" ] && printf '\n%s' "${model_effort# }"
fi

exit 0
