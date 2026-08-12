#!/usr/bin/env bash
# Dokploy helper for the Drishti deployment on server.codeation.io.
#
# Subcommands:
#   status                     — list containers with state/status
#   health <service>           — show healthcheck details for a container
#   logs <service> [tail]      — fetch container logs (default 100 lines)
#   get-compose [out.json]     — save current compose entity to a file (default /tmp/compose.json)
#   env-list                   — list env var KEYS set on the compose (values hidden)
#   env-set KEY=VALUE          — add or replace one env var on the compose
#   deploy <title>             — trigger compose.deploy with the given title
#   poll [seconds]             — poll container states until api+web are running (default 300s)
#   restart <service>          — restart a running container by service name
#   raw GET  <path> [--query k=v ...]
#   raw POST <path> <body.json>
#
# A "service" name is the compose service (api, web), i.e. the suffix
# between the app prefix and "-1" in the container name.
#
# Credentials live in deploy/dokploy.env (gitignored). Copy
# deploy/dokploy.env.example and fill it in, or export the vars yourself:
#   DOKPLOY_API, DOKPLOY_KEY, DOKPLOY_COMPOSE_ID, DOKPLOY_APP

set -euo pipefail

ENV_FILE="$(dirname "$0")/dokploy.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

: "${DOKPLOY_API:?set DOKPLOY_API (see deploy/dokploy.env.example)}"
: "${DOKPLOY_KEY:?set DOKPLOY_KEY (see deploy/dokploy.env.example)}"
: "${DOKPLOY_COMPOSE_ID:?set DOKPLOY_COMPOSE_ID (see deploy/dokploy.env.example)}"
: "${DOKPLOY_APP:?set DOKPLOY_APP (see deploy/dokploy.env.example)}"

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

container_id() {
  local svc="$1"
  curl -fsS -H "x-api-key: $DOKPLOY_KEY" \
    "$DOKPLOY_API/docker.getContainersByAppNameMatch?appName=$DOKPLOY_APP" \
  | python3 -c "
import json,sys
name='$DOKPLOY_APP-$svc-1'
for c in json.load(sys.stdin):
    if c['name']==name:
        print(c['containerId'])
        break"
}

cmd_status() {
  curl -fsS -H "x-api-key: $DOKPLOY_KEY" \
    "$DOKPLOY_API/docker.getContainersByAppNameMatch?appName=$DOKPLOY_APP" \
  | python3 -c "
import json,sys
for c in sorted(json.load(sys.stdin), key=lambda c: c['name']):
    print(f\"  {c['name']:60s}  {c['state']:12s}  {c['status']}\")"
}

cmd_health() {
  local svc="$1"
  local id; id="$(container_id "$svc")"
  [ -n "$id" ] || { echo "container not found: $svc" >&2; exit 1; }
  curl -fsS -H "x-api-key: $DOKPLOY_KEY" "$DOKPLOY_API/docker.getConfig?containerId=$id" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
h=d.get('State',{}).get('Health') or {}
print('Status:', h.get('Status'))
print('FailingStreak:', h.get('FailingStreak'))
print('Test:', d.get('Config',{}).get('Healthcheck',{}).get('Test'))
print()
for i,e in enumerate((h.get('Log') or [])[-3:]):
    print(f'--- entry {i} exit={e.get(\"ExitCode\")} ---')
    print((e.get('Output') or '').rstrip())"
}

cmd_logs() {
  local svc="$1" tail="${2:-100}"
  curl -fsS -H "x-api-key: $DOKPLOY_KEY" --get \
    --data-urlencode "composeId=$DOKPLOY_COMPOSE_ID" \
    --data-urlencode "containerId=$DOKPLOY_APP-$svc-1" \
    --data-urlencode "tail=$tail" \
    "$DOKPLOY_API/compose.readLogs" \
  | python3 -c "
import json,sys
s=json.load(sys.stdin)
for l in s.split('\n'):
    if l.strip():
        p=l.split(' ',1)
        print(p[1] if len(p)>1 else l)"
}

cmd_get_compose() {
  local out="${1:-/tmp/compose.json}"
  curl -fsS -H "x-api-key: $DOKPLOY_KEY" \
    "$DOKPLOY_API/compose.one?composeId=$DOKPLOY_COMPOSE_ID" > "$out"
  echo "wrote $out ($(wc -c <"$out") bytes)"
}

cmd_env_list() {
  curl -fsS -H "x-api-key: $DOKPLOY_KEY" \
    "$DOKPLOY_API/compose.one?composeId=$DOKPLOY_COMPOSE_ID" \
  | python3 -c "
import json,sys
env=json.load(sys.stdin).get('env') or ''
for line in env.splitlines():
    line=line.strip()
    if line and not line.startswith('#'):
        print(line.split('=',1)[0])"
}

cmd_env_set() {
  # Adds or replaces one KEY=VALUE in the compose env, preserving the rest.
  local kv="$1"
  case "$kv" in *=*) ;; *) echo "usage: env-set KEY=VALUE" >&2; exit 1 ;; esac
  local cur=/tmp/compose.cur.json
  curl -fsS -H "x-api-key: $DOKPLOY_KEY" \
    "$DOKPLOY_API/compose.one?composeId=$DOKPLOY_COMPOSE_ID" > "$cur"
  KV="$kv" python3 -c "
import json,os,sys
c=json.load(open('$cur'))
key,value=os.environ['KV'].split('=',1)
lines=[l for l in (c.get('env') or '').splitlines() if l.strip()]
lines=[l for l in lines if l.split('=',1)[0].strip()!=key]
lines.append(f'{key}={value}')
payload={
    'composeId': c['composeId'],
    'name': c['name'],
    'appName': c['appName'],
    'description': c.get('description') or '',
    'env': '\n'.join(lines),
    'sourceType': c['sourceType'],
    'composeType': c['composeType'],
    'composePath': c.get('composePath') or './docker-compose.yml',
}
open('/tmp/compose.update.json','w').write(json.dumps(payload))
sys.stderr.write(f'env: set {key} ({len(lines)} vars total)\n')
"
  curl -fsS -X POST \
    -H "x-api-key: $DOKPLOY_KEY" \
    -H "Content-Type: application/json" \
    --data @/tmp/compose.update.json \
    "$DOKPLOY_API/compose.update" -o /dev/null -w "compose.update: HTTP %{http_code}\n"
  rm -f /tmp/compose.update.json
}

cmd_deploy() {
  local title="${1:-Manual deployment}"
  local body
  body=$(DOKPLOY_COMPOSE_ID="$DOKPLOY_COMPOSE_ID" TITLE="$title" python3 -c "
import json,os
print(json.dumps({'composeId': os.environ['DOKPLOY_COMPOSE_ID'], 'title': os.environ['TITLE']}))")
  curl -fsS -X POST \
    -H "x-api-key: $DOKPLOY_KEY" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "$DOKPLOY_API/compose.deploy"
  echo
}

cmd_poll() {
  local timeout="${1:-300}"
  local start elapsed summary
  start=$(date +%s)
  while :; do
    elapsed=$(( $(date +%s) - start ))
    if [ "$elapsed" -gt "$timeout" ]; then
      echo "[$elapsed s] timeout"; break
    fi
    summary=$(curl -fsS -H "x-api-key: $DOKPLOY_KEY" \
      "$DOKPLOY_API/docker.getContainersByAppNameMatch?appName=$DOKPLOY_APP" \
    | python3 -c "
import json,sys
d={c['name'].replace('$DOKPLOY_APP-','').replace('-1',''): (c['state'],c['status']) for c in json.load(sys.stdin)}
def f(k): s=d.get(k,('?','?')); return f'{s[0]}/{s[1][:35]}'
print(f\"api={f('api')} | web={f('web')}\")")
    echo "[${elapsed}s] $summary"
    case "$summary" in
      *"api=running/Up"*"(healthy)"*) echo "== API HEALTHY ==";       break ;;
      *"api=exited"*)                 echo "== API EXITED (check logs) =="; break ;;
    esac
    sleep 15
  done
}

cmd_restart() {
  local svc="$1"
  local id; id="$(container_id "$svc")"
  [ -n "$id" ] || { echo "container not found: $svc" >&2; exit 1; }
  curl -fsS -X POST \
    -H "x-api-key: $DOKPLOY_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"containerId\":\"$id\"}" \
    "$DOKPLOY_API/docker.restartContainer"
  echo
}

cmd_raw() {
  local method="$1" path="$2"; shift 2
  case "$method" in
    GET)
      local qs=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --query) qs+="${qs:+&}$2"; shift 2 ;;
          *) echo "unknown arg: $1" >&2; exit 1 ;;
        esac
      done
      curl -fsS -H "x-api-key: $DOKPLOY_KEY" "$DOKPLOY_API/$path${qs:+?$qs}" | python3 -m json.tool
      ;;
    POST)
      local body="$1"
      curl -fsS -X POST \
        -H "x-api-key: $DOKPLOY_KEY" \
        -H "Content-Type: application/json" \
        --data @"$body" "$DOKPLOY_API/$path" | python3 -m json.tool
      ;;
    *) echo "unknown method: $method" >&2; exit 1 ;;
  esac
}

sub="${1:-}"; shift || true
case "$sub" in
  status)      cmd_status "$@" ;;
  health)      cmd_health "$@" ;;
  logs)        cmd_logs   "$@" ;;
  get-compose) cmd_get_compose "$@" ;;
  env-list)    cmd_env_list "$@" ;;
  env-set)     cmd_env_set "$@" ;;
  deploy)      cmd_deploy "$@" ;;
  poll)        cmd_poll   "$@" ;;
  restart)     cmd_restart "$@" ;;
  raw)         cmd_raw    "$@" ;;
  ""|-h|--help|help) usage ;;
  *) echo "unknown subcommand: $sub" >&2; usage ;;
esac
