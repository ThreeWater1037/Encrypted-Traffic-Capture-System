#!/usr/bin/env bash
# Git-based Ubuntu deployment and migration: Master + Vue frontend, root services.
set -Eeuo pipefail
umask 077
PROJECT_DIR=/data/project/Encrypted-Traffic-Capture-System
CONDA_ROOT=/data/miniconda
CONDA_ENV=/data/miniconda/envs/Encrypted-Traffic-Capture-System-Master
WEB_PORT=5173
REPO_URL=https://github.com/ThreeWater1037/Encrypted-Traffic-Capture-System.git
BRANCH=main
WORKER_WAS_ACTIVE=0
SERVICE_DIR=/etc/traffic-console
while (($#)); do
  case "$1" in
    --help|-h)
      cat <<'HELP'
Usage: bash deploy_master_frontend_git_root.sh [options]
  --project-dir PATH  Default /data/project/Encrypted-Traffic-Capture-System
  --conda-root PATH   Default /data/miniconda
  --conda-env PATH    Default /data/miniconda/envs/Encrypted-Traffic-Capture-System-Master
  --web-port PORT     Default 5173 (Master stays on 127.0.0.1:5200)
Requires root, Ubuntu amd64, systemd and GitHub access. Uses repository branch main.
Creates the entire environment, production frontend build, Nginx and Master services.
Migrates a non-Git deployment in place; existing Git checkouts require clean tracked files and fast-forward history.
Configuration and task data are preserved; never uses git reset --hard or git clean.
Re-running restarts Master and the console; run during a maintenance window.
Browser login: generated admin password in /etc/traffic-console/login.json.
An active Worker sharing this exact project directory is stopped during checkout and restarted on success.
Does not format disks, change SSH or enable UFW.
Cloud security-group rules still require your cloud console.
HELP
      exit 0 ;;
    --project-dir|--conda-root|--conda-env|--web-port)
      (($# >= 2)) || { echo "Missing value for $1" >&2; exit 1; }
      case "$1" in
        --project-dir) PROJECT_DIR=$2 ;;
        --conda-root) CONDA_ROOT=$2 ;;
        --conda-env) CONDA_ENV=$2 ;;
        --web-port) WEB_PORT=$2 ;;
      esac
      shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n=== %s ===\n' "$*"; }
[[ $EUID == 0 ]] || die 'Run as root.'
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die 'Requires Linux x86_64.'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu && -d /run/systemd/system ]] || die 'Requires Ubuntu with systemd.'
[[ $WEB_PORT =~ ^[0-9]+$ ]] || die 'Invalid web port.'
WEB_PORT=$((10#$WEB_PORT))
((WEB_PORT >= 1 && WEB_PORT <= 65535 && WEB_PORT != 5200 && WEB_PORT != 5100 && WEB_PORT != 22)) || die 'Invalid/conflicting web port.'
for path in "$PROJECT_DIR" "$CONDA_ROOT" "$CONDA_ENV"; do
  [[ $path =~ ^/[a-zA-Z0-9_./-]+$ ]] || die 'Use absolute paths without spaces.'
done
PROJECT_DIR=$(realpath -m "$PROJECT_DIR")
CONDA_ROOT=$(realpath -m "$CONDA_ROOT")
CONDA_ENV=$(realpath -m "$CONDA_ENV")
for path in "$PROJECT_DIR" "$CONDA_ROOT" "$CONDA_ENV"; do
  [[ $path != / ]] || die 'Refusing root directory as installation target.'
done
exec 9>/run/lock/traffic-console-deploy.lock
flock -n 9 || die 'Another console deployment is running.'
STAMP=$(date -u +%Y%m%dT%H%M%SZ)-$$
LOG=/var/log/traffic-console-deploy-$STAMP.log
BACKUP=/var/backups/traffic-console/$STAMP
TMP_DIR=$(mktemp -d /tmp/traffic-console-deploy.XXXXXXXX)
install -d -m 700 "$BACKUP"
touch "$LOG"
chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
cleanup() {
  code=$?
  trap - EXIT
  rm -rf -- "$TMP_DIR"
  if ((code)); then
    printf '\nDeployment failed. Log: %s\nBackup: %s\n' "$LOG" "$BACKUP"
    printf 'Inspect: journalctl -u traffic-master -u traffic-console-nginx -n 100 --no-pager\n'
    printf 'Services (including a same-directory Worker) may be stopped or partially deployed. Fix the error and re-run.\n'
  fi
  exit "$code"
}
trap cleanup EXIT
trap 'echo "Failed at line $LINENO" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM
backup() { if [[ -e $1 ]]; then cp -a -- "$1" "$BACKUP/$(basename "$1")"; fi; }
download() { curl -fL --retry 3 --connect-timeout 20 --max-time 900 "$1" -o "$2"; }

step 'Install system packages'
export DEBIAN_FRONTEND=noninteractive HOME=/root
NGINX_PREEXISTED=0
if systemctl cat nginx.service >/dev/null 2>&1; then NGINX_PREEXISTED=1; fi
apt-get update
apt-get install -y git python3 curl ca-certificates bzip2 nginx apache2-utils iproute2
# A fresh package may start its default port-80 site. Our dedicated instance uses WEB_PORT.
if ((! NGINX_PREEXISTED)); then systemctl disable --now nginx; fi

step 'Clone and validate the Git deployment target'
export GIT_TERMINAL_PROMPT=0
if [[ -e $PROJECT_DIR/.git && ! -d $PROJECT_DIR/.git ]]; then die 'Linked worktrees are not supported for this deployment.'; fi
if [[ -d $PROJECT_DIR/.git ]]; then
  [[ $(git -C "$PROJECT_DIR" remote get-url origin) == "$REPO_URL" ]] || die 'Existing origin differs from the configured repository.'
  [[ $(git -C "$PROJECT_DIR" branch --show-current) == "$BRANCH" ]] || die 'Existing checkout is not on main; switch deliberately before deployment.'
  [[ -z $(git -C "$PROJECT_DIR" status --porcelain --untracked-files=no) ]] || die 'Tracked files have local changes; commit/stash them yourself before deployment.'
  git -C "$PROJECT_DIR" fetch origin "$BRANCH"
fi
git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$TMP_DIR/source"
TARGET_COMMIT=$(git -C "$TMP_DIR/source" rev-parse HEAD)
if [[ -d $PROJECT_DIR/.git ]]; then
  git -C "$PROJECT_DIR" fetch "$TMP_DIR/source" "$TARGET_COMMIT"
  git -C "$PROJECT_DIR" merge-base --is-ancestor HEAD "$TARGET_COMMIT" || die 'Local history diverges or is ahead; refusing non-fast-forward deployment.'
fi
for file in requirements-master.txt master_server/__main__.py frontend/package.json frontend/package-lock.json; do
  [[ -f $TMP_DIR/source/$file ]] || die "Repository is missing $file";
done
# Runtime state must never become tracked source during deployment.
if git -C "$TMP_DIR/source" ls-files | grep -Eq '(^|/)(master\.ya?ml|worker\.ya?ml|\.env([^/]*))$|^(master_data|worker_data)/|^frontend/(releases|node_modules|dist)/'; then
  die 'Repository tracks deployment configuration, secrets or runtime data. Remove them from Git before deployment.'
fi
printf 'Deploying commit: %s\n' "$TARGET_COMMIT"
printf '%s\n' "$TARGET_COMMIT" > "$BACKUP/target-commit.txt"

step 'Install/reuse Miniconda and dedicated Master/Node environment'
if [[ ! -x $CONDA_ROOT/bin/conda ]]; then
  [[ ! -e $CONDA_ROOT ]] || die "Incomplete Conda root: $CONDA_ROOT"
  mkdir -p "$(dirname "$CONDA_ROOT")"
  download https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh "$TMP_DIR/miniconda.sh"
  bash "$TMP_DIR/miniconda.sh" -b -p "$CONDA_ROOT"
fi
if [[ ! -x $CONDA_ENV/bin/python ]]; then
  [[ ! -e $CONDA_ENV ]] || die "Incomplete Conda environment: $CONDA_ENV"
  "$CONDA_ROOT/bin/conda" create -y --prefix "$CONDA_ENV" --override-channels -c conda-forge python=3.12 nodejs=22 pip
else
  # Stop before changing an environment used by this service.
  if systemctl cat traffic-master.service >/dev/null 2>&1; then systemctl stop traffic-master; fi
  "$CONDA_ROOT/bin/conda" install -y --prefix "$CONDA_ENV" --override-channels -c conda-forge python=3.12 nodejs=22 pip
fi
export PATH="$CONDA_ENV/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PYTHON=$CONDA_ENV/bin/python
"$PYTHON" -m pip install -r "$TMP_DIR/source/requirements-master.txt"
"$PYTHON" -m pip check
node --version
npm --version

step 'Build production frontend with same-origin API'
(
  cd "$TMP_DIR/source/frontend"
  npm ci --include=dev --no-audit --no-fund
  VITE_MASTER_API=/api/v1 VITE_MASTER_TOKEN= npm run build
)
[[ -s $TMP_DIR/source/frontend/dist/index.html ]] || die 'Frontend build did not produce index.html.'

step 'Stop managed services and verify ports'
systemctl daemon-reload
for service in traffic-console-nginx traffic-master; do
  if systemctl cat "$service.service" >/dev/null 2>&1; then systemctl stop "$service"; fi
done
for port in 5200 "$WEB_PORT"; do
  if ss -lntH "sport = :$port" | grep -q .; then
    die "Port $port is occupied by another service/manual process; resolve it before re-running."
  fi
done

step 'Back up and adopt/update the Git checkout'
install -d -m 755 "$PROJECT_DIR"
for dir in frontend master_server; do
  [[ ! -L $PROJECT_DIR/$dir ]] || die "Refusing source symlink: $PROJECT_DIR/$dir"
done
if systemctl is-active --quiet traffic-worker && [[ $(systemctl show traffic-worker -p WorkingDirectory --value) == "$PROJECT_DIR" ]]; then
  WORKER_WAS_ACTIVE=1
  systemctl stop traffic-worker
fi
if [[ -d $PROJECT_DIR/.git ]]; then
  git -C "$PROJECT_DIR" rev-parse HEAD > "$BACKUP/previous-commit.txt"
  # Git refuses conflicting untracked files; no forced checkout or data cleanup.
  git -C "$PROJECT_DIR" merge --ff-only "$TARGET_COMMIT"
else
  # Preserve old files that the cloned repository is about to replace, including hidden files.
  git -C "$TMP_DIR/source" ls-files -z > "$TMP_DIR/tracked-files"
  EXISTING=()
  while IFS= read -r -d '' file; do
    [[ ! -L $PROJECT_DIR/$file ]] || die "Refusing destination symlink: $file"
    if [[ -e $PROJECT_DIR/$file ]]; then EXISTING+=("$file"); fi
  done < "$TMP_DIR/tracked-files"
  if ((${#EXISTING[@]})); then tar -czf "$BACKUP/source-before-git.tar.gz" -C "$PROJECT_DIR" -- "${EXISTING[@]}"; fi
  git -C "$TMP_DIR/source" archive HEAD > "$TMP_DIR/tracked-source.tar"
  tar -xf "$TMP_DIR/tracked-source.tar" -C "$PROJECT_DIR" --no-same-owner
  cp -a "$TMP_DIR/source/.git" "$PROJECT_DIR/.git"
fi
# Local exclusions do not modify the repository's tracked .gitignore.
cat >> "$PROJECT_DIR/.git/info/exclude" <<'EOF'

# traffic-console deployment runtime
/master.yaml
/worker.yaml
/master_data/
/worker_data/
/frontend/releases/
/frontend/node_modules/
/frontend/dist/
/frontend/.env*
EOF
[[ $(git -C "$PROJECT_DIR" rev-parse HEAD) == "$TARGET_COMMIT" ]] || die 'Checkout does not match the built commit.'
[[ -z $(git -C "$PROJECT_DIR" status --porcelain --untracked-files=no) ]] || die 'Checkout has unexpected tracked modifications.'
# Publish each frontend build in its own directory; old builds remain available for rollback.
WEB_ROOT=$PROJECT_DIR/frontend/releases/$STAMP
install -d -m 755 "$WEB_ROOT"
cp -a "$TMP_DIR/source/frontend/dist/." "$WEB_ROOT/"

step 'Create Master config, private credentials and Nginx config'
install -d -m 700 "$SERVICE_DIR"
backup "$PROJECT_DIR/master.yaml"
backup "$SERVICE_DIR/nginx.conf"
backup "$SERVICE_DIR/login.json"
backup "$SERVICE_DIR/htpasswd"
export PROJECT_DIR PYTHON WEB_PORT WEB_ROOT SERVICE_DIR
"$PYTHON" - <<'PY'
import json, os, re, secrets, subprocess
from pathlib import Path
import yaml
project = Path(os.environ['PROJECT_DIR'])
config = project / 'master.yaml'
d = yaml.safe_load(config.read_text(encoding='utf-8')) if config.exists() else {}
if d is None: d = {}
if not isinstance(d, dict): raise SystemExit('master.yaml must be a mapping.')
def section(name):
    value = d.setdefault(name, {})
    if not isinstance(value, dict): raise SystemExit('Invalid config section: ' + name)
    return value
server = section('server')
token = server.get('token') or secrets.token_hex(32)
# The token is inserted into a quoted Nginx directive. Reject unsafe legacy characters.
if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~-]+', token):
    raise SystemExit('Existing Master token contains characters unsuitable for Nginx. Set a random hexadecimal token, then retry.')
server.update(host='127.0.0.1', port=5200, token=token)
paths = section('paths')
data = Path(os.path.expandvars(str(paths.get('data_dir') or './master_data'))).expanduser()
if not data.is_absolute(): data = project / data
data = data.resolve(); data.mkdir(parents=True, exist_ok=True)
paths['data_dir'] = str(data)
limits = section('limits')
for key, value in dict(max_content_length=268435456, max_items=100000, max_queue_size=100,
                       worker_request_timeout=15, poll_interval=2).items():
    limits.setdefault(key, value)
# New installation starts with no Workers; preserve an existing explicit bootstrap setting.
section('bootstrap_worker').setdefault('enabled', False)
temp = config.with_suffix('.yaml.deploy-tmp')
temp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding='utf-8')
temp.chmod(0o600); temp.replace(config)
service = Path(os.environ['SERVICE_DIR'])
login_file = service / 'login.json'
login = json.loads(login_file.read_text()) if login_file.exists() else {'username': 'admin', 'password': secrets.token_urlsafe(24)}
if login.get('username') != 'admin' or not re.fullmatch(r'[A-Za-z0-9_-]+', login.get('password', '')):
    raise SystemExit('Invalid existing login.json; expected generated admin credentials.')
login_file.write_text(json.dumps(login, indent=2) + '\n'); login_file.chmod(0o600)
subprocess.run(['htpasswd', '-ciB', str(service / 'htpasswd'), 'admin'], input=login['password'] + '\n', text=True, check=True, stdout=subprocess.DEVNULL)
(service / 'htpasswd').chmod(0o600)
nginx = '''user root;
worker_processes auto;
pid /run/traffic-console-nginx.pid;
error_log /var/log/traffic-console-nginx-error.log;
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /var/log/traffic-console-nginx-access.log;
    sendfile on;
    server_tokens off;
    server {
        listen __PORT__;
        server_name _;
        root __ROOT__;
        index index.html;
        client_max_body_size __MAX_BODY__;
        auth_basic "Traffic Capture Console";
        auth_basic_user_file /etc/traffic-console/htpasswd;
        location /api/ {
            proxy_pass http://127.0.0.1:5200;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Authorization "Bearer __TOKEN__";
            proxy_read_timeout 120s;
            proxy_send_timeout 120s;
            proxy_buffering off;
            add_header Cache-Control "no-store" always;
        }
        location /assets/ { try_files $uri =404; }
        location / { try_files $uri $uri/ /index.html; add_header Cache-Control "no-store"; }
    }
}
'''
nginx = nginx.replace('__PORT__', os.environ['WEB_PORT']).replace('__ROOT__', os.environ['WEB_ROOT'])
nginx = nginx.replace('__TOKEN__', token).replace('__MAX_BODY__', str(int(limits['max_content_length'])))
(service / 'nginx.conf').write_text(nginx); (service / 'nginx.conf').chmod(0o600)
print('Master data directory:', data)
print('Browser credentials saved to /etc/traffic-console/login.json (not printed to deployment log).')
PY

step 'Install root systemd services'
backup /etc/systemd/system/traffic-master.service
backup /etc/systemd/system/traffic-console-nginx.service
cat > /etc/systemd/system/traffic-master.service <<EOF
[Unit]
Description=Traffic Capture Master
Wants=network-online.target
After=network-online.target
RequiresMountsFor=$PROJECT_DIR $CONDA_ENV
[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$PROJECT_DIR
Environment=HOME=/root
Environment=PYTHONUNBUFFERED=1
Environment=MASTER_CONFIG_FILE=$PROJECT_DIR/master.yaml
Environment=PATH=$CONDA_ENV/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$PYTHON -m master_server
Restart=always
RestartSec=5
TimeoutStopSec=60
KillMode=mixed
UMask=0077
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/traffic-console-nginx.service <<EOF
[Unit]
Description=Traffic Capture Frontend and API Proxy
Wants=network-online.target traffic-master.service
After=network-online.target traffic-master.service
RequiresMountsFor=$WEB_ROOT
[Service]
Type=simple
User=root
Group=root
ExecStartPre=/usr/sbin/nginx -t -c /etc/traffic-console/nginx.conf
ExecStart=/usr/sbin/nginx -c /etc/traffic-console/nginx.conf -g "daemon off;"
ExecReload=/bin/kill -HUP \$MAINPID
KillSignal=SIGQUIT
Restart=always
RestartSec=5
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
EOF
chmod 644 /etc/systemd/system/traffic-master.service /etc/systemd/system/traffic-console-nginx.service
/usr/sbin/nginx -t -c "$SERVICE_DIR/nginx.conf"
systemd-analyze verify /etc/systemd/system/traffic-master.service /etc/systemd/system/traffic-console-nginx.service
systemctl daemon-reload
systemctl enable traffic-master traffic-console-nginx
systemctl restart traffic-master traffic-console-nginx

step 'Verify direct Master, browser authentication, frontend assets and reverse proxy'
"$PYTHON" - <<'PY'
import base64, json, os, re, time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
import yaml
d = yaml.safe_load((Path(os.environ['PROJECT_DIR']) / 'master.yaml').read_text())
login = json.loads((Path(os.environ['SERVICE_DIR']) / 'login.json').read_text())
basic = 'Basic ' + base64.b64encode((login['username'] + ':' + login['password']).encode()).decode()
opener = build_opener(ProxyHandler({}))
def get(url, auth=None):
    req = Request(url, headers={'Authorization': auth} if auth else {})
    with opener.open(req, timeout=10) as r: return r.read()
local = 'http://127.0.0.1:5200/api/v1'
web = 'http://127.0.0.1:' + os.environ['WEB_PORT']
for _ in range(30):
    try:
        h = json.loads(get(local + '/health'))
        assert h['service'] == 'traffic-capture-master' and h['status'] == 'ok'
        get(web + '/', basic)
        break
    except Exception: time.sleep(2)
else: raise SystemExit('Services failed to become ready. Inspect systemd logs.')
for url in (web + '/', web + '/api/v1/health', web + '/api/v1/machines', local + '/machines'):
    try: get(url)
    except HTTPError as e: assert e.code == 401, f'Unexpected authentication response: {e.code}'
    else: raise AssertionError('Unauthenticated access unexpectedly allowed: ' + url)
json.loads(get(local + '/machines', 'Bearer ' + d['server']['token']))
assert json.loads(get(web + '/api/v1/health', basic))['status'] == 'ok'
assert 'machines' in json.loads(get(web + '/api/v1/machines', basic))
html = get(web + '/', basic).decode()
assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
assert assets, 'No frontend assets referenced.'
for path in assets:
    content = get(web + path, basic)
    assert content, 'Empty frontend asset.'
    if len(d['server']['token']) >= 24:
        assert d['server']['token'].encode() not in content, 'Master Token leaked into frontend bundle.'
print('PASS: Master health/auth, browser login protection, proxy API and production assets.')
PY
systemctl is-enabled traffic-master traffic-console-nginx
systemctl is-active traffic-master traffic-console-nginx
if ((WORKER_WAS_ACTIVE)); then systemctl start traffic-worker; fi
printf '\nDONE. Open http://<server-IP>:%s\n' "$WEB_PORT"
printf 'Read browser login: cat /etc/traffic-console/login.json\n'
printf 'Cloud security group: allow TCP %s from your admin IP /32; do not expose 5200.\n' "$WEB_PORT"
printf 'HTTP does not encrypt browser credentials; use trusted access/VPN or add HTTPS for public use.\n'
printf 'No cloud firewall/SSH/UFW changes made. Add Workers using their :5100 URL and Token in the console.\n'
printf 'Deployment log: %s\nBackups: %s\n' "$LOG" "$BACKUP"
exit 0
