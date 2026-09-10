#!/usr/bin/env bash
# Git-based Ubuntu root Worker deployment: first install, migration and updates.
# Official package instructions: https://support.mozilla.org/en-US/kb/install-firefox-linux
# Miniconda: https://www.anaconda.com/docs/getting-started/advanced-install/silent-mode
set -Eeuo pipefail
umask 077

PROJECT_DIR=/data/project/Encrypted-Traffic-Capture-System
CONDA_ROOT=/data/miniconda
CONDA_ENV=/data/miniconda/envs/Encrypted-Traffic-Capture-System
MASTER_IP=
WORKER_ID_OVERRIDE=
SMOKE_URL=https://example.com
SMOKE_TIMEOUT=600
SKIP_SMOKE=0
ALLOW_INTERRUPT=0
ROTATE_TOKEN=0
REPO_URL=https://github.com/ThreeWater1037/Encrypted-Traffic-Capture-System.git
BRANCH=main

usage() {
  cat <<'HELP'
Usage: bash deploy_worker_git_root.sh [options]
  --project-dir PATH     Default: /data/project/Encrypted-Traffic-Capture-System
  --conda-root PATH      Default: /data/miniconda
  --conda-env PATH       Default: /data/miniconda/envs/Encrypted-Traffic-Capture-System
  --worker-id ID         Preserve existing ID by default; new: Ubuntu-worker-01
  --master-ip IPv4       Add a narrow UFW rule; print cloud security-group instructions
  --smoke-url HTTPS_URL  Default: https://example.com (choose an accessible test site)
  --smoke-timeout SEC    Default: 600 seconds, includes driver downloads and queue waits
  --skip-smoke           Skip real Chrome/Firefox capture; NOT a full acceptance check
  --allow-interrupt      Allow restarting an existing Worker with pending tasks
  --rotate-token         Generate a new Token; update Master afterwards
  --help                Show this help

Requires root, Ubuntu amd64 and systemd. No uploaded project or existing Python/Conda needed.
Clones GitHub main. Migrates a non-Git directory with source backups.
Re-running updates a clean Git checkout using fast-forward only.
Does not reset/clean Git or delete task data. Same-directory Master deployment is refused.
Existing configuration is backed up; Token, proxy and data directory are preserved.
Uses Google Chrome + Mozilla DEB Firefox. Does not remove Snap or existing task data.
Does not enable UFW or alter SSH rules. Cloud security groups require console setup.
The script stops the Worker while changing dependencies. On failure, inspect its log.
HELP
}
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n=== %s ===\n' "$*"; }
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --skip-smoke) SKIP_SMOKE=1; shift ;;
    --allow-interrupt) ALLOW_INTERRUPT=1; shift ;;
    --rotate-token) ROTATE_TOKEN=1; shift ;;
    --project-dir|--conda-root|--conda-env|--master-ip|--worker-id|--smoke-url|--smoke-timeout)
      (($# >= 2)) || die "Missing value for $1"
      case "$1" in
        --project-dir) PROJECT_DIR=$2 ;;
        --conda-root) CONDA_ROOT=$2 ;;
        --conda-env) CONDA_ENV=$2 ;;
        --master-ip) MASTER_IP=$2 ;;
        --worker-id) WORKER_ID_OVERRIDE=$2 ;;
        --smoke-url) SMOKE_URL=$2 ;;
        --smoke-timeout) SMOKE_TIMEOUT=$2 ;;
      esac
      shift 2 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

[[ $EUID == 0 ]] || die 'Run as root.'
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die 'Requires Linux x86_64.'
[[ -f /etc/os-release ]] || die 'Missing /etc/os-release.'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu ]] || die 'This script supports Ubuntu only.'
[[ -d /run/systemd/system ]] || die 'Requires a running systemd host, not a bare container.'
[[ $SMOKE_TIMEOUT =~ ^[0-9]+$ && $SMOKE_TIMEOUT -ge 60 ]] || die 'Smoke timeout must be >= 60.'
for path in "$PROJECT_DIR" "$CONDA_ROOT" "$CONDA_ENV"; do
  [[ $path =~ ^/[a-zA-Z0-9_./-]+$ && $path != / ]] || die "Use an absolute path without spaces: $path"
done
PROJECT_DIR=$(realpath -m "$PROJECT_DIR")
CONDA_ROOT=$(realpath -m "$CONDA_ROOT")
CONDA_ENV=$(realpath -m "$CONDA_ENV")
[[ $CONDA_ROOT != / && $CONDA_ENV != / && $PROJECT_DIR != / ]] || die 'Root directory is not a valid target.'
for utility in flock tar gzip awk realpath; do
  command -v "$utility" >/dev/null || die "Missing Ubuntu base utility: $utility"
done
exec 9>/run/lock/traffic-worker-deploy.lock
flock -n 9 || die 'Another Worker deployment is running.'
exec 8>/run/lock/traffic-console-deploy.lock
flock -n 8 || die 'A Master/frontend deployment is running.'

STAMP=$(date -u +%Y%m%dT%H%M%SZ)-$$
LOG=/var/log/traffic-worker-deploy-$STAMP.log
touch "$LOG"
chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
TMP_DIR=$(mktemp -d /tmp/traffic-worker-deploy.XXXXXXXX)
BACKUP_DIR=/var/backups/traffic-worker/$STAMP
install -d -m 700 "$BACKUP_DIR"
cleanup() {
  local code=$?
  trap - EXIT
  rm -rf -- "$TMP_DIR"
  if ((code != 0)); then
    printf '\nDeployment incomplete (exit %s). Log: %s\n' "$code" "$LOG"
    printf 'Backups: %s\nWorker may be stopped or running without passing acceptance.\n' "$BACKUP_DIR"
    printf 'Inspect: systemctl status traffic-worker; journalctl -u traffic-worker -n 100 --no-pager\n'
  fi
  exit "$code"
}
trap cleanup EXIT
trap 'printf "Failed at line %s\n" "$LINENO" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM
backup() {
  if [[ -e $1 ]]; then
    cp -a -- "$1" "$BACKUP_DIR/$(basename "$1")"
  fi
}
download() { curl --fail --location --retry 3 --connect-timeout 20 --max-time 900 "$1" -o "$2"; }

step 'Check deployment layout and install Git bootstrap dependencies'
systemctl daemon-reload
# Updating a shared checkout would also change Master code without rebuilding its frontend.
if systemctl cat traffic-master.service >/dev/null 2>&1 && [[ $(systemctl show traffic-master -p WorkingDirectory --value) == "$PROJECT_DIR" ]]; then
  die 'Master uses this checkout. Deploy Worker in a separate --project-dir, or update the shared stack with a coordinated deployment.'
fi
export DEBIAN_FRONTEND=noninteractive HOME=/root
apt-get update
apt-get install -y git python3 python3-yaml curl ca-certificates

step 'Clone and validate the Git deployment target'
export GIT_TERMINAL_PROMPT=0
if [[ -L $PROJECT_DIR/.git || ( -e $PROJECT_DIR/.git && ! -d $PROJECT_DIR/.git ) ]]; then die 'Linked worktrees are not supported for this deployment.'; fi
if [[ -d $PROJECT_DIR/.git ]]; then
  [[ $(git -C "$PROJECT_DIR" remote get-url origin) == "$REPO_URL" ]] || die 'Existing origin differs from the configured repository.'
  [[ $(git -C "$PROJECT_DIR" branch --show-current) == "$BRANCH" ]] || die 'Existing checkout is not on main; switch deliberately before deployment.'
  [[ -z $(git -C "$PROJECT_DIR" status --porcelain --untracked-files=no) ]] || die 'Tracked files have local changes; commit/stash them yourself before deployment.'
  git -C "$PROJECT_DIR" ls-files > "$TMP_DIR/current-files.txt"
  if grep -Eq '(^|/)(master\.ya?ml|worker\.ya?ml|\.env([^/]*))$|^(master_data|worker_data)/|^frontend/(releases|node_modules|dist)/' "$TMP_DIR/current-files.txt"; then
    die 'Current checkout tracks configuration or runtime data; move that state out of Git before updating.'
  fi
  git -C "$PROJECT_DIR" fetch origin "$BRANCH"
fi
git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$TMP_DIR/source"
TARGET_COMMIT=$(git -C "$TMP_DIR/source" rev-parse HEAD)
if [[ -d $PROJECT_DIR/.git ]]; then
  git -C "$PROJECT_DIR" fetch "$TMP_DIR/source" "$TARGET_COMMIT"
  git -C "$PROJECT_DIR" merge-base --is-ancestor HEAD "$TARGET_COMMIT" || die 'Local history diverges or is ahead; refusing non-fast-forward deployment.'
fi
for file in worker_agent/__main__.py requirements-worker.txt wiki_fetcher.py browser_discovery.py browser_proxy.py batch_process.py extract_features.py classify_packets.py infer_packets.py; do
  [[ -f $TMP_DIR/source/$file ]] || die "Repository is missing $file";
done
# Runtime state must never become tracked source during deployment.
git -C "$TMP_DIR/source" ls-files > "$TMP_DIR/source-files.txt"
if grep -Eq '(^|/)(master\.ya?ml|worker\.ya?ml|\.env([^/]*))$|^(master_data|worker_data)/|^frontend/(releases|node_modules|dist)/' "$TMP_DIR/source-files.txt"; then
  die 'Repository tracks deployment configuration, secrets or runtime data. Remove them from Git before deployment.'
fi
printf 'Deploying commit: %s\n' "$TARGET_COMMIT"
printf '%s\n' "$TARGET_COMMIT" > "$BACKUP_DIR/target-commit.txt"

step 'Protect deployment configuration and data paths'
export PROJECT_DIR
python3 - "$TMP_DIR/source-files.txt" <<'PY'
import os, subprocess, sys
from pathlib import Path
import yaml
root = Path(os.environ['PROJECT_DIR'])
source = Path(sys.argv[1]).parent / 'source'
tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode().split('\0')
if (root / '.git').is_dir():
    tracked += subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
protected = []
for name in ('worker.yaml', 'worker.yml', 'master.yaml', 'master.yml'):
    config = root / name
    if config.is_symlink():
        raise SystemExit('Refusing configuration symlink: ' + str(config))
    if config.exists():
        data = yaml.safe_load(config.read_text(encoding='utf-8')) or {}
        if not isinstance(data, dict) or not isinstance(data.get('paths', {}), dict):
            raise SystemExit('Invalid configuration: ' + str(config))
        value = data.get('paths', {}).get('data_dir')
        if value:
            path = Path(os.path.expandvars(str(value))).expanduser()
            protected.append((path if path.is_absolute() else root / path).resolve())
for name in filter(None, tracked):
    destination = root / name
    for path in (destination, *destination.parents):
        if path == root:
            break
        if path.is_symlink():
            raise SystemExit('Refusing source destination symlink: ' + str(path))
    resolved = destination.resolve()
    if any(resolved == data or data in resolved.parents or resolved in data.parents for data in protected):
        raise SystemExit('Tracked source conflicts with configured data_dir: ' + name)
    if destination.is_dir():
        raise SystemExit('Tracked file would replace an existing directory: ' + str(destination))
PY

step 'Check existing service'
if systemctl is-active --quiet traffic-worker; then
  if (( ! ALLOW_INTERRUPT )); then
    command -v curl >/dev/null && command -v python3 >/dev/null || die 'Cannot inspect current Worker; use --allow-interrupt only when appropriate.'
    curl --noproxy '*' -fsS --max-time 10 http://127.0.0.1:5100/api/v1/health > "$TMP_DIR/health.json" || die 'Cannot inspect current Worker; stop it explicitly or use --allow-interrupt.'
    python3 - "$TMP_DIR/health.json" <<'PY'
import json, sys
h = json.load(open(sys.argv[1]))
if h.get('status') != 'ok' or h.get('busy') or h.get('queue_size', 0):
    raise SystemExit('Worker has pending work. Wait, or explicitly use --allow-interrupt.')
PY
  fi
fi
systemctl daemon-reload
if systemctl cat traffic-worker.service >/dev/null 2>&1; then
  systemctl stop traffic-worker
fi
if command -v ss >/dev/null && ss -lntH 'sport = :5100' | grep -q .; then
  die 'Port 5100 is still occupied; stop the manually launched Worker first.'
fi

step 'Back up and adopt/update the Git checkout'
install -d -m 755 "$PROJECT_DIR"
for dir in frontend master_server worker_agent; do
  [[ ! -L $PROJECT_DIR/$dir ]] || die "Refusing source symlink: $PROJECT_DIR/$dir"
done
if [[ -d $PROJECT_DIR/.git ]]; then
  git -C "$PROJECT_DIR" rev-parse HEAD > "$BACKUP_DIR/previous-commit.txt"
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
  if ((${#EXISTING[@]})); then tar -czf "$BACKUP_DIR/source-before-git.tar.gz" -C "$PROJECT_DIR" -- "${EXISTING[@]}"; fi
  git -C "$TMP_DIR/source" archive HEAD > "$TMP_DIR/tracked-source.tar"
  tar -xf "$TMP_DIR/tracked-source.tar" -C "$PROJECT_DIR" --no-same-owner
  cp -a "$TMP_DIR/source/.git" "$PROJECT_DIR/.git"
fi
# Local exclusions do not modify the repository's tracked .gitignore.
cat >> "$PROJECT_DIR/.git/info/exclude" <<'EOF'

# traffic-worker deployment runtime
/master.yaml
/worker.yaml
/worker.yml
/master.yml
/master_data/
/worker_data/
/frontend/releases/
/frontend/node_modules/
/frontend/dist/
/frontend/.env*
EOF
[[ $(git -C "$PROJECT_DIR" rev-parse HEAD) == "$TARGET_COMMIT" ]] || die 'Checkout does not match the selected commit.'
[[ -z $(git -C "$PROJECT_DIR" status --porcelain --untracked-files=no) ]] || die 'Checkout has unexpected tracked modifications.'

step 'Install Ubuntu dependencies and TShark'
export DEBIAN_FRONTEND=noninteractive
printf 'wireshark-common wireshark-common/install-setuid boolean false\n' | debconf-set-selections
apt-get update
apt-get install -y python3 curl ca-certificates gnupg tshark fonts-noto-cjk ufw nano bzip2 iproute2
python3 - "$MASTER_IP" "$SMOKE_URL" <<'PY'
import ipaddress, sys
from urllib.parse import urlsplit
if sys.argv[1]:
    ipaddress.IPv4Address(sys.argv[1])
u = urlsplit(sys.argv[2])
if u.scheme != 'https' or not u.hostname or u.username or u.password:
    raise SystemExit('--smoke-url must be an HTTPS URL without embedded credentials.')
PY

step 'Install or reuse Miniconda'
if [[ ! -x $CONDA_ROOT/bin/conda ]]; then
  [[ ! -e $CONDA_ROOT ]] || die "Incomplete Conda directory: $CONDA_ROOT; inspect before retrying."
  mkdir -p "$(dirname "$CONDA_ROOT")"
  download https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh "$TMP_DIR/miniconda.sh"
  bash "$TMP_DIR/miniconda.sh" -b -p "$CONDA_ROOT"
fi
export HOME=/root
if [[ ! -x $CONDA_ENV/bin/python ]]; then
  [[ ! -e $CONDA_ENV ]] || die "Incomplete environment: $CONDA_ENV; inspect before retrying."
  "$CONDA_ROOT/bin/conda" create -y --prefix "$CONDA_ENV" --override-channels -c conda-forge python=3.12 pip
fi
PYTHON=$CONDA_ENV/bin/python
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python >= 3.10 required"; print(sys.version)'
"$PYTHON" -m pip install -r "$PROJECT_DIR/requirements-worker.txt" selenium webdriver-manager
"$PYTHON" -m pip check

step 'Install or reuse Google Chrome'
if [[ ! -x /usr/bin/google-chrome ]]; then
  download https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb "$TMP_DIR/chrome.deb"
  chmod 755 "$TMP_DIR"
  chmod 644 "$TMP_DIR/chrome.deb"
  apt-get install -y "$TMP_DIR/chrome.deb"
  chmod 700 "$TMP_DIR"
fi
/usr/bin/google-chrome --version

step 'Install Mozilla DEB Firefox (avoid Snap/standalone GeckoDriver mismatch)'
install -d -m 755 /etc/apt/keyrings
download https://packages.mozilla.org/apt/repo-signing-key.gpg "$TMP_DIR/mozilla.asc"
FINGERPRINT=$(gpg --batch --show-keys --with-colons "$TMP_DIR/mozilla.asc" | awk -F: '$1 == "fpr" {print $10; exit}')
[[ $FINGERPRINT == 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 ]] || die 'Mozilla signing key fingerprint mismatch.'
backup /etc/apt/keyrings/packages.mozilla.org.asc
install -m 644 "$TMP_DIR/mozilla.asc" /etc/apt/keyrings/packages.mozilla.org.asc
# Reuse the same filenames as the manual procedure. Do not silently delete other sources.
if [[ -f /etc/apt/sources.list.d/mozilla.list ]] && grep -q 'packages.mozilla.org' /etc/apt/sources.list.d/mozilla.list; then
  die 'Existing mozilla.list found. Consolidate it with mozilla.sources before retrying to avoid duplicate/conflicting sources.'
fi
backup /etc/apt/sources.list.d/mozilla.sources
backup /etc/apt/preferences.d/mozilla
cat > /etc/apt/sources.list.d/mozilla.sources <<'EOF'
Types: deb
URIs: https://packages.mozilla.org/apt
Suites: mozilla
Components: main
Signed-By: /etc/apt/keyrings/packages.mozilla.org.asc
EOF
cat > /etc/apt/preferences.d/mozilla <<'EOF'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000

Package: firefox
Pin: release o=Ubuntu
Pin-Priority: -1
EOF
chmod 644 /etc/apt/sources.list.d/mozilla.sources /etc/apt/preferences.d/mozilla
apt-get update
apt-cache policy firefox
# Ubuntu's epoch-prefixed Snap transition package makes the DEB switch a package downgrade.
apt-get install -y --allow-downgrades firefox
[[ -x /usr/lib/firefox/firefox ]] || die 'Mozilla Firefox binary missing after install.'
/usr/lib/firefox/firefox --version

step 'Check root capture and headless browser startup'
tshark -D
tshark -i any -a duration:3 -w "$TMP_DIR/capture-test.pcapng"
mkdir "$TMP_DIR/chrome-profile"
timeout 45s /usr/bin/google-chrome --headless=new --no-sandbox --disable-dev-shm-usage \
  --user-data-dir="$TMP_DIR/chrome-profile" --dump-dom about:blank > "$TMP_DIR/chrome.log" 2>&1 || {
  cat "$TMP_DIR/chrome.log"; die 'Chrome headless startup failed.';
}
timeout 45s /usr/lib/firefox/firefox --headless --screenshot "$TMP_DIR/firefox.png" about:blank \
  > "$TMP_DIR/firefox.log" 2>&1 || { cat "$TMP_DIR/firefox.log"; die 'Firefox headless startup failed.'; }
[[ -s $TMP_DIR/firefox.png ]] || die 'Firefox did not produce its screenshot.'

step 'Back up and update worker.yaml'
CONFIG_FILE=$PROJECT_DIR/worker.yaml
backup "$CONFIG_FILE"
export PROJECT_DIR PYTHON CONFIG_FILE WORKER_ID_OVERRIDE ROTATE_TOKEN
"$PYTHON" - <<'PY'
import os, secrets
from pathlib import Path
import yaml
p = Path(os.environ['CONFIG_FILE'])
d = yaml.safe_load(p.read_text(encoding='utf-8')) if p.exists() else {}
if d is None:
    d = {}
if not isinstance(d, dict):
    raise SystemExit('Existing worker.yaml must be a mapping.')
def section(name):
    v = d.setdefault(name, {})
    if not isinstance(v, dict):
        raise SystemExit(f'Invalid YAML section: {name}')
    return v
w = section('worker')
w['id'] = os.environ['WORKER_ID_OVERRIDE'] or w.get('id') or 'Ubuntu-worker-01'
w.update(host='0.0.0.0', port=5100)
old_token = w.get('token')
if not old_token or os.environ['ROTATE_TOKEN'] == '1':
    w['token'] = secrets.token_hex(32)
    print('Generated a new Token. Read it from worker.yaml when adding this Worker to Master.')
elif isinstance(old_token, str) and len(old_token) < 24:
    print('Preserved existing short Token to keep Master connected; --rotate-token replaces it explicitly.')
paths = section('paths')
data = paths.get('data_dir') or './worker_data'
data_path = Path(os.path.expandvars(str(data))).expanduser()
if not data_path.is_absolute():
    data_path = p.parent / data_path
data_path = data_path.resolve()
data_path.mkdir(parents=True, exist_ok=True)
paths.update(project_root=os.environ['PROJECT_DIR'], python_executable=os.environ['PYTHON'], data_dir=str(data_path))
for key, value in dict(max_queue_size=10, max_items=100000, task_timeout_seconds=0, max_content_length=268435456).items():
    section('limits').setdefault(key, value)
section('browsers').update(chrome_binary='/usr/bin/google-chrome', firefox_binary='/usr/lib/firefox/firefox')
# Keep existing Edge, CORS and proxy settings.
section('network').setdefault('proxy_url', None)
temp = p.with_name(p.name + '.deploy-tmp')
temp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding='utf-8')
temp.chmod(0o600)
temp.replace(p)
print('Data directory:', data_path)
PY

step 'Install root systemd service'
UNIT=/etc/systemd/system/traffic-worker.service
backup "$UNIT"
cat > "$UNIT" <<EOF
[Unit]
Description=Encrypted Traffic Capture Worker
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
Environment=WORKER_CONFIG_FILE=$CONFIG_FILE
Environment=PATH=$CONDA_ENV/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$PYTHON -m worker_agent
Restart=always
RestartSec=5
TimeoutStopSec=60
KillMode=mixed
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
systemd-analyze verify "$UNIT"
systemctl daemon-reload
systemctl enable traffic-worker
systemctl restart traffic-worker

step 'Verify API and real Chrome/Firefox captures'
export SKIP_SMOKE SMOKE_URL SMOKE_TIMEOUT
"$PYTHON" - <<'PY'
import json, os, time, uuid
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
import yaml
d = yaml.safe_load(Path(os.environ['CONFIG_FILE']).read_text(encoding='utf-8'))
token = d['worker']['token']
opener = build_opener(ProxyHandler({}))  # Local API must not use an external proxy.
def api(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request('http://127.0.0.1:5100/api/v1' + path, data=data,
                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with opener.open(req, timeout=15) as response:
        return json.load(response)
for attempt in range(30):
    try:
        health = api('/health')
        if health.get('status') == 'ok':
            break
    except Exception:
        pass
    time.sleep(2)
else:
    raise SystemExit('Worker health failed. Inspect journalctl -u traffic-worker.')
assert health['worker_id'] == d['worker']['id'], 'Unexpected Worker identity on port 5100.'
caps = api('/capabilities')
assert Path(caps['python']['executable']).resolve() == Path(os.environ['PYTHON']).resolve(), 'Wrong task Python.'
browser_paths = {b['name']: Path(b['path']).resolve() for b in caps['browsers']}
assert browser_paths.get('firefox') == Path('/usr/lib/firefox/firefox').resolve(), 'Wrong Firefox binary; check service environment overrides.'
assert browser_paths.get('chrome') == Path('/usr/bin/google-chrome').resolve(), 'Wrong Chrome binary.'
assert caps['capture']['pcap'], 'TShark not detected.'
print('Health, authentication, task Python and browser paths verified.', flush=True)
if os.environ['SKIP_SMOKE'] == '1':
    print('SKIPPED real capture: service deployed, capture acceptance NOT verified.', flush=True)
    raise SystemExit(0)
task_id = 'deploy-smoke-' + uuid.uuid4().hex
payload = {'task_id': task_id, 'items': [{'id': '1', 'name': 'deployment-test', 'url': os.environ['SMOKE_URL']}],
           'browsers': ['chrome', 'firefox'], 'pcap': True,
           'outputs': {'html': False, 'reports': False},
           'analysis': {'steps': [], 'with_coframe': False, 'sni_suffixes': []}}
api('/tasks', payload)
task_dir = Path(d['paths']['data_dir']) / 'tasks' / task_id
print('Capture test:', task_id, '\nTask log:', task_dir / 'worker.log', flush=True)
deadline = time.monotonic() + int(os.environ['SMOKE_TIMEOUT'])
done = False
last = None
try:
    while time.monotonic() < deadline:
        task = api('/tasks/' + task_id + '?include_result=false')
        status = task['status']
        if status != last:
            print('Capture status:', status, flush=True)
            last = status
        if status in {'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'}:
            done = True
            if status != 'SUCCEEDED':
                raise RuntimeError(f'Capture did not pass: {status}. Inspect {task_dir / "worker.log"}')
            manifest = json.loads((task_dir / 'manifest.json').read_text(encoding='utf-8'))
            units = manifest.get('units', [])
            assert len(units) == 2 and all(u.get('status') == 'SUCCEEDED' for u in units), 'Incomplete capture units.'
            for browser in ('chrome', 'firefox'):
                assert any(f.stat().st_size > 0 for f in task_dir.rglob('tls_keys_' + browser + '.log')), f'Missing {browser} TLS keys.'
                assert any(f.stat().st_size > 24 for f in task_dir.rglob('capture_' + browser + '.pcap')), f'Missing {browser} PCAP.'
            print('PASS: Chrome and Firefox captures SUCCEEDED, manifest and artifacts verified.', flush=True)
            break
        time.sleep(3)
    else:
        raise TimeoutError('Smoke test timed out; check network, driver downloads or queued tasks.')
finally:
    if not done:
        try:
            api('/tasks/' + task_id + '/cancel', {})  # Cancel only this deployment test.
        except Exception as exc:
            print('Could not cancel deployment test:', type(exc).__name__, flush=True)
PY

step 'Network access and final status'
if [[ -n $MASTER_IP ]]; then
  ufw allow from "$MASTER_IP" to any port 5100 proto tcp comment 'Traffic Worker Master'
  printf 'Cloud security group: INBOUND ALLOW TCP 5100, source %s/32\n' "$MASTER_IP"
else
  printf 'Cloud security group: INBOUND ALLOW TCP 5100 from your Master egress IPv4 /32.\n'
  printf 'No UFW rules changed. Re-run with --master-ip IPv4 if needed.\n'
fi
ufw status
printf 'UFW was not enabled automatically; preserve SSH access before enabling it.\n'
systemctl is-enabled traffic-worker
systemctl is-active traffic-worker
printf '\nDeployment finished. Configuration: %s\nBackups: %s\nLog: %s\n' "$CONFIG_FILE" "$BACKUP_DIR" "$LOG"
printf 'Git commit: %s\n' "$TARGET_COMMIT"
printf 'Master URL: http://<Worker-IP>:5100 (do not append /api/v1).\n'
printf 'Read the Token locally from worker.yaml; it was not printed to this deployment log.\n'
printf 'Direct Worker smoke tasks do not appear in the Master task list.\n'
exit 0
