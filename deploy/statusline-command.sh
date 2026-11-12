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

pct_color() {
  if [ "$1" -lt 60 ]; then echo 82
  elif [ "$1" -le 85 ]; then echo 220
  else echo 196; fi
}

rate_badge() {
  case "$2" in ''|*[!0-9]*) return 0;; esac
  printf " \033[38;5;%sm· %s %s%%%s\033[0m" "$(pct_color "$2")" "$1" "$2" "${3:-}"
}

ctx_badge() {
  case "$1" in ''|*[!0-9]*) return 0;; esac
  case "$2" in ''|*[!0-9]*) return 0;; esac
  case "$3" in ''|0|*[!0-9]*) return 0;; esac
  used_k=$(( ($2 + 500) / 1000 ))
  if [ $(( $3 % 1000000 )) -eq 0 ]; then size_h="$(( $3 / 1000000 ))M"
  elif [ $(( $3 % 1000 )) -eq 0 ]; then size_h="$(( $3 / 1000 ))k"
  else size_h="$3"; fi
  printf " \033[38;5;%sm· ctx %s%% %sk/%s\033[0m" "$(pct_color "$1")" "$1" "$used_k" "$size_h"
}

five_hour_pct=$(echo "$input" | jq -r '(.rate_limits.five_hour.used_percentage // empty) | round' 2>/dev/null)
seven_day_pct=$(echo "$input" | jq -r '(.rate_limits.seven_day.used_percentage // empty) | round' 2>/dev/null)
ctx_pct=$(echo "$input" | jq -r '(.context_window.used_percentage // empty) | round' 2>/dev/null)
ctx_used=$(echo "$input" | jq -r '(.context_window.total_input_tokens // empty) | round' 2>/dev/null)
ctx_size=$(echo "$input" | jq -r '(.context_window.context_window_size // empty) | round' 2>/dev/null)

ccd="${CLAUDE_CONFIG_DIR:-}"
if [ -z "$ccd" ] || [ "$ccd" = "$HOME/.claude" ]; then
  cfg_label="default"
else
  cfg_base=$(basename "$ccd")
  cfg_label="${cfg_base#.claude-}"
fi
cfg_badge=$(printf " \033[38;5;245m· cfg:%s\033[0m" "$cfg_label")

cfg_json="${ccd:-$HOME}/.claude.json"
now=$(date +%s)
scoped=$(jq -c -s '
  (.[0] // {}) as $f
  | ($f.cachedUsageUtilization // {}) as $c
  | (($c.accountUuid | type) == "string" and $c.accountUuid == $f.oauthAccount.accountUuid
     and ($c.utilization.limits | type) == "array") as $ok
  | {at: (if $ok and (($c.fetchedAtMs | type) == "number") then ($c.fetchedAtMs / 1000 | floor) else null end),
     rows: (if $ok then
             [ $c.utilization.limits[]
               | select(.kind == "weekly_scoped"
                        and (.scope.model.display_name | type) == "string"
                        and (.percent | type) == "number")
               | {name: .scope.model.display_name,
                  pct: (.percent | round),
                  resets_at: (.resets_at
                              | if type == "string"
                                then (try (sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdate) catch null)
                                elif type == "number" then . else null end)} ]
           else [] end)}' "$cfg_json" 2>/dev/null)
if ! jq -n -e --argjson s "$scoped" '$s | type == "object" and (.rows | type) == "array"' >/dev/null 2>&1; then
  scoped='{"at":null,"rows":[]}'
fi
scoped_at=$(printf '%s' "$scoped" | jq -r '.at // empty' 2>/dev/null)
model_badges=$(printf '%s' "$scoped" \
  | jq -r '.rows[] | [.name, .pct, (.resets_at // "")] | join("\u001f")' 2>/dev/null \
  | while IFS=$(printf '\037') read -r name pct reset; do
      case "$reset" in
        (''|*[!0-9]*) ;;
        (*) [ "$now" -ge "$reset" ] && continue ;;
      esac
      suffix=""
      case "$scoped_at" in
        (''|*[!0-9]*) ;;
        (*)
          age=$(( now - scoped_at ))
          if [ "$age" -ge 86400 ]; then suffix=" ($(( age / 86400 ))d)"
          elif [ "$age" -ge 3600 ]; then suffix=" ($(( age / 3600 ))h)"; fi
          ;;
      esac
      rate_badge "$name" "$pct" "$suffix"
    done)
ratelimits="$(rate_badge 5h "$five_hour_pct")$(rate_badge 7d "$seven_day_pct")$model_badges$(ctx_badge "$ctx_pct" "$ctx_used" "$ctx_size")"

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
        if echo "$input" | jq -c --argjson ts "$now" --argjson scoped "$scoped" '{
              five_hour_pct: (.rate_limits.five_hour.used_percentage // null),
              seven_day_pct: (.rate_limits.seven_day.used_percentage // null),
              five_hour_resets_at: (.rate_limits.five_hour.resets_at // null),
              seven_day_resets_at: (.rate_limits.seven_day.resets_at // null),
              ts: $ts,
              model_scoped: $scoped.rows,
              model_scoped_at: $scoped.at
            }' > "$usage_tmp" 2>/dev/null; then
          mv -f "$usage_tmp" "$usage_dir/$usage_acct.json" 2>/dev/null || rm -f "$usage_tmp" 2>/dev/null
        else
          rm -f "$usage_tmp" 2>/dev/null
        fi
      fi
      ;;
  esac
fi

case "$session_id" in
  ""|.*|*/*) : ;;
  *)
    ctx_root="$HOME/.claude/dockwright/usage"
    [ -d "$ctx_root" ] || { [ -d "$HOME/.claude/orchestrator/usage" ] && ctx_root="$HOME/.claude/orchestrator/usage"; }
    ctx_dir="$ctx_root/context"
    ctx_file="$ctx_dir/$session_id.json"
    ctx_writable=1
    case "$ctx_pct" in ''|*[!0-9]*) ctx_writable=0;; esac
    case "$ctx_used" in ''|*[!0-9]*) ctx_writable=0;; esac
    case "$ctx_size" in ''|*[!0-9]*) ctx_writable=0;; esac
    if [ "$ctx_writable" -eq 1 ]; then
      if mkdir -p "$ctx_dir" 2>/dev/null; then
        ctx_tmp="$ctx_dir/.$session_id.json.tmp.$$"
        if printf '{"used_tokens":%s,"context_window_size":%s,"used_percentage":%s,"ts":%s}\n' \
             "$ctx_used" "$ctx_size" "$ctx_pct" "$(date +%s)" > "$ctx_tmp" 2>/dev/null; then
          mv -f "$ctx_tmp" "$ctx_file" 2>/dev/null || rm -f "$ctx_tmp" 2>/dev/null
        else
          rm -f "$ctx_tmp" 2>/dev/null
        fi
      fi
    else
      rm -f "$ctx_file" 2>/dev/null
    fi
    ;;
esac

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
elif [ "$agent" = "worker" ]; then
  funny_name=""
  task_name=""
  parent_name=""
  if [ -n "$record" ]; then
    funny_name=$(jq -r '.funny_name // empty' "$record" 2>/dev/null)
    task_name=$(jq -r '.name // empty' "$record" 2>/dev/null)
    parent_name=$(jq -r '.parent_name // .parent_manager_name // empty' "$record" 2>/dev/null)
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
  row1="$row1$proposals$todos"
  row2="$ratelimits$cfg_badge$model_effort"
  printf '%s\n' "$row1"
  printf '%s' "${row2# }"
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
