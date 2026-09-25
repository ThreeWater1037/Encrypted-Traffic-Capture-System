"""Isolated checks for the uploadable Windows deployment script; never install software."""
from __future__ import annotations

import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deployment' / 'deploy_worker_windows.ps1'


def embedded(name):
    source = SCRIPT.read_text(encoding='utf-8')
    match = re.search(r'\$' + name + r" = @'\n(.*?)\n'@", source, re.S)
    if not match:
        raise AssertionError('Missing embedded code: ' + name)
    return match.group(1)


def load_helper():
    module = types.ModuleType('deployment_helper')
    exec(compile(embedded('helper'), str(SCRIPT), 'exec'), module.__dict__)
    return module


class DeploymentFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        self.run = self.root / 'run'
        self.run.mkdir()
        self.config = self.project / 'worker.yaml'
        (self.run / 'tracked.json').write_text(json.dumps(['worker_agent/config.py', 'wiki_fetcher.py']))
        self.runtime = dict(project=str(self.project), run=str(self.run),
                            python=str(self.root / 'miniconda/envs/worker/python.exe'),
                            worker_id='', rotate_token=False,
                            browsers={b: str(self.root / (b + '.exe')) for b in ('chrome', 'edge', 'firefox')})
        self.helper = load_helper()
        self.env_patch = patch.dict(os.environ)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Configuration validation must use YAML, just as the deployment process does.
        for name in list(os.environ):
            if name.startswith('WORKER_') or name in {'PROJECT_ROOT', 'PYTHON_EXECUTABLE', 'CHROME_BINARY', 'EDGE_BINARY', 'FIREFOX_BINARY', 'MAX_QUEUE_SIZE', 'MAX_ITEMS', 'TASK_TIMEOUT_SECONDS', 'MAX_CONTENT_LENGTH'}:
                del os.environ[name]
        sys.path.insert(0, str(ROOT))
        self.addCleanup(lambda: sys.path.remove(str(ROOT)))

    def save(self, value):
        self.config.write_text(yaml.safe_dump(value), encoding='utf-8')


class DeploymentConfigTests(DeploymentFixture, unittest.TestCase):
    def test_preserves_token_id_proxy_limits_and_data(self):
        self.save(dict(worker=dict(id='existing', token='secret-existing'),
                       paths=dict(data_dir='../capture data'), limits=dict(max_items=123),
                       network=dict(proxy_url='http://127.0.0.1:7890'),
                       cors=dict(allowed_origins=['https://console.example.com'])))
        self.helper.prepare(self.runtime)
        result = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        self.assertEqual(result['worker']['token'], 'secret-existing')
        self.assertEqual(result['worker']['id'], 'existing')
        self.assertEqual(result['paths']['data_dir'], str((self.root / 'capture data').resolve()))
        self.assertEqual(result['limits']['max_items'], 123)
        self.assertEqual(result['network']['proxy_url'], 'http://127.0.0.1:7890')
        self.assertEqual(result['cors']['allowed_origins'], ['https://console.example.com'])

    def test_new_token_and_explicit_rotation(self):
        self.helper.prepare(self.runtime)
        token = yaml.safe_load(self.config.read_text())['worker']['token']
        self.assertEqual(len(token), 64)
        self.helper.prepare(self.runtime)
        self.assertEqual(yaml.safe_load(self.config.read_text())['worker']['token'], token)
        self.runtime.update(rotate_token=True, worker_id='win-02')
        self.helper.prepare(self.runtime)
        result = yaml.safe_load(self.config.read_text())
        self.assertNotEqual(result['worker']['token'], token)
        self.assertEqual(result['worker']['id'], 'win-02')

    def test_rejects_source_and_deployment_data_overlap_without_modifying_config(self):
        for path in ('.', 'worker_agent', 'wiki_fetcher.py/child', '../run', '../miniconda', '.git/objects'):
            with self.subTest(path=path):
                self.save(dict(paths=dict(data_dir=path)))
                previous = self.config.read_bytes()
                with self.assertRaises(RuntimeError):
                    self.helper.prepare(self.runtime, check_only=True)
                self.assertEqual(self.config.read_bytes(), previous)

    def test_precheck_does_not_write_or_create_data(self):
        self.save(dict(paths=dict(data_dir='../new-data')))
        previous = self.config.read_bytes()
        self.helper.prepare(self.runtime, check_only=True)
        self.assertEqual(self.config.read_bytes(), previous)
        self.assertFalse((self.root / 'new-data').exists())

    def test_invalid_yaml_does_not_replace_configuration(self):
        for value in (['not a mapping'], {'worker': 'not a section'}, {'unknown': {}}, {'limits': {'max_items': -1}}):
            self.save(value)
            previous = self.config.read_bytes()
            with self.assertRaises((ValueError, RuntimeError)):
                self.helper.prepare(self.runtime)
            self.assertEqual(self.config.read_bytes(), previous)


class AcceptanceTests(DeploymentFixture, unittest.TestCase):
    def run_acceptance(self, *, missing_key=False, status='SUCCEEDED', skip=False, open_auth=False, timeout=False):
        self.runtime.update(skip_smoke=skip, smoke_url='https://example.com', smoke_timeout=60)
        self.helper.prepare(self.runtime)
        d = yaml.safe_load(self.config.read_text())
        task_dir = Path(d['paths']['data_dir']) / 'tasks' / 'deploy-smoke-fixed'
        task_dir.mkdir(parents=True)
        browsers = list(self.runtime['browsers'])
        (task_dir / 'manifest.json').write_text(json.dumps({'units': [dict(browser=b, status='SUCCEEDED') for b in browsers]}))
        for browser in browsers:
            (task_dir / ('capture_' + browser + '.pcap')).write_bytes(b'p' * 100)
            if not (missing_key and browser == 'edge'):
                (task_dir / ('tls_keys_' + browser + '.log')).write_text('CLIENT_RANDOM dummy')
        calls = []
        self.api_calls = calls
        def respond(request, **kwargs):
            path = request.full_url.split('/api/v1')[1]
            calls.append(path)
            if path == '/health':
                result = dict(status='ok', worker_id=d['worker']['id'])
            elif path == '/capabilities':
                if not request.has_header('Authorization') and not open_auth:
                    raise HTTPError(request.full_url, 401, 'Unauthorized', {}, None)
                result = dict(python=dict(executable=self.runtime['python']), capture=dict(pcap=True),
                              browsers=[dict(name=b, path=p) for b, p in self.runtime['browsers'].items()])
            else:
                result = dict(status=status)
            return io.StringIO(json.dumps(result))
        opener = types.SimpleNamespace(open=respond)
        with patch.object(self.helper, 'build_opener', return_value=opener), \
             patch.object(self.helper.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='fixed')), \
             patch.object(self.helper.time, 'monotonic', side_effect=[0, 100] if timeout else [0, 1]):
            self.helper.verify(self.runtime)
        return calls

    def test_success_checks_all_browser_artifacts(self):
        self.assertIn('/tasks', self.run_acceptance())

    def test_missing_browser_keylog_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'Missing TLS keylog: edge'):
            self.run_acceptance(missing_key=True)

    def test_partial_status_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'Capture failed: PARTIAL'):
            self.run_acceptance(status='PARTIAL')

    def test_skip_still_checks_api_but_does_not_submit(self):
        self.assertNotIn('/tasks', self.run_acceptance(skip=True))

    def test_unauthenticated_access_fails_acceptance(self):
        with self.assertRaisesRegex(RuntimeError, 'unauthenticated'):
            self.run_acceptance(open_auth=True)

    def test_timeout_cancels_only_deployment_task(self):
        with self.assertRaises(TimeoutError):
            self.run_acceptance(timeout=True)
        self.assertEqual([p for p in self.api_calls if p.endswith('/cancel')], ['/tasks/deploy-smoke-fixed/cancel'])


class RunnerEnvironmentTests(unittest.TestCase):
    def test_three_explicit_local_drivers_reach_worker_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drivers = {name: str(root / exe) for name, exe in (
                ('CHROMEDRIVER_PATH', 'chromedriver.exe'), ('EDGEDRIVER_PATH', 'msedgedriver.exe'),
                ('GECKODRIVER_PATH', 'geckodriver.exe'))}
            runtime = dict(project=str(root), python=sys.executable, logs=str(root / 'logs'),
                           tshark=str(root / 'tshark.exe'), drivers=drivers)
            (root / 'runtime.json').write_text(json.dumps(runtime), encoding='utf-8')
            module = types.ModuleType('deployment_runner')
            module.__file__ = str(root / 'run_worker.py')
            exec(compile(embedded('runnerCode'), str(SCRIPT), 'exec'), module.__dict__)
            with patch.object(module, 'contain_process_tree', return_value=1), \
                 patch.object(module.subprocess, 'run') as run, \
                 patch.dict(os.environ, {'CHROMEDRIVER_PATH': 'wrong-old-driver', 'WORKER_TOKEN': 'old-token'}):
                self.assertEqual(module.main(), 1)
                env = run.call_args.kwargs['env']
                for name, path in drivers.items():
                    self.assertEqual(env[name], path)
                self.assertNotIn('WORKER_TOKEN', env)


@unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell HTTP JSON semantics')
class MirrorHttpTests(unittest.TestCase):
    def test_real_rest_json_arrays_for_git_firefox_and_edge(self):
        installer_bytes = b'fixture installer bytes; never executed'
        installer_sha256 = hashlib.sha256(installer_bytes).hexdigest()
        download_requests = []
        responses = {
            '/git-for-windows/': [{'name': 'v2.9.0.windows.1/'}, {'name': 'v2.55.0.windows.5/'}],
            '/git-for-windows/v2.55.0.windows.5/': [{'name': 'Git-2.55.0.5-64-bit.exe', 'url': 'https://fixture.example/git.exe'}],
            '/firefox/': [{'name': '156.0.1/'}, {'name': '157.0b1/'}],
            '/firefox/156.0.1/win64/zh-CN/': [{'name': 'Firefox Setup 156.0.1.msi', 'url': 'https://fixture.example/Firefox Setup 156.0.1.msi'}],
            '/edge': [{'Product': 'Dev'}, {'Product': 'Stable', 'Releases': [{'Platform': 'Windows', 'Architecture': 'x64'}]}],
            '/empty/git-for-windows/': [],
            '/bad/firefox/': {'error': 'Mirror unavailable'},
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/Firefox%20Setup%20156.0.1.msi':
                    download_requests.append(self.path)
                    body = installer_bytes
                else:
                    body = json.dumps(responses[self.path]).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'mirror_http.ps1'
                script_path = str(SCRIPT).replace("'", "''")
                url = f'http://127.0.0.1:{server.server_port}'
                path.write_text(f"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1"
Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1"
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script_path}',[ref]$tokens,[ref]$errors)
foreach ($name in @('Get-Metadata','Get-BinaryListing','Resolve-GitMirrorUrl','Resolve-FirefoxMirrorUrl','Get-Installer','Assert-InstallerFile','Assert-SignedInstaller','ConvertTo-HttpsDownloadUrl')) {{
    $node=$ast.Find({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }},$true)
    . ([scriptblock]::Create($node.Extent.Text))
}}
$Sources=@{{BinaryMirror='{url}'; GitUrl=''; FirefoxMsiUrl=''}}
if ((Resolve-GitMirrorUrl) -ne 'https://fixture.example/git.exe') {{ throw 'Git selection failed' }}
if ((Resolve-FirefoxMirrorUrl) -ne 'https://fixture.example/Firefox Setup 156.0.1.msi') {{ throw 'Firefox selection failed' }}
# Keep production URL validation, resolution, caching and hash checking intact.
# Only transport host/scheme is redirected to our actual local HTTP server.
function Invoke-WebRequest {{
    param($Uri, [switch]$UseBasicParsing, $OutFile, $TimeoutSec)
    if ($Uri -cne 'https://fixture.example/Firefox%20Setup%20156.0.1.msi') {{ throw 'Wrong downloader URL' }}
    Microsoft.PowerShell.Utility\\Invoke-WebRequest -Uri ($Uri.Replace('https://fixture.example', '{url}')) -UseBasicParsing -OutFile $OutFile -TimeoutSec $TimeoutSec
}}
$CacheDir = Join-Path $PSScriptRoot 'download cache'
$InstallerDirectory = ''
New-Item -ItemType Directory -Path $CacheDir | Out-Null
$downloaded = Get-Installer 'firefox.msi' {{ Resolve-FirefoxMirrorUrl }} -ExpectedSha256 '{installer_sha256}'
if ((Get-FileHash -LiteralPath $downloaded).Hash -ne '{installer_sha256}') {{ throw 'Downloaded bytes differ' }}
$stable=@(Get-Metadata '{url}/edge' | Where-Object Product -eq 'Stable')
if ($stable.Count -ne 1 -or $stable[0].Releases[0].Architecture -ne 'x64') {{ throw 'Edge array filtering failed' }}
$Sources.BinaryMirror='{url}/empty'
$failed=$false
try {{ Resolve-GitMirrorUrl }} catch {{
    if ($_.Exception.Message -notlike '*no stable releases*') {{ throw }}
    $failed=$true
}}
if (-not $failed) {{ throw 'Empty mirror was accepted' }}
$Sources.BinaryMirror='{url}/bad'
$failed=$false
try {{ Resolve-FirefoxMirrorUrl }} catch {{
    if ($_.Exception.Message -notlike '*file listing with name fields*') {{ throw }}
    $failed=$true
}}
if (-not $failed) {{ throw 'Malformed mirror response was accepted' }}
Write-Output 'HTTP JSON arrays passed'
""", encoding='utf-8-sig')
                process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                                         capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                self.assertIn('HTTP JSON arrays passed', process.stdout)
                self.assertEqual(download_requests, ['/Firefox%20Setup%20156.0.1.msi'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@unittest.skipUnless(sys.platform == 'win32' and shutil.which('git'), 'Windows PowerShell and Git')
class GitPathTests(unittest.TestCase):
    def test_real_git_unicode_paths_survive_powershell_codepage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repository with spaces'
            repo.mkdir()
            git = shutil.which('git')

            def run_git(*arguments):
                return subprocess.run([git, '-C', str(repo), *arguments], check=True, capture_output=True)

            run_git('init', '-b', 'main')
            names = ['使用说明.md', '目录/有 空格的说明.md', 'notes with spaces.md', 'plain.txt']
            for name in names:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', encoding='utf-8')
            run_git('add', '--all')
            run_git('-c', 'user.name=Deployment Test', '-c', 'user.email=deployment@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-m', 'path fixtures')
            run_git('update-ref', 'refs/remotes/origin/main', 'HEAD')
            raw = run_git('-c', 'core.quotepath=true', 'ls-tree', '-r', '--name-only', 'HEAD').stdout
            self.assertIn(b'"\\', raw, 'Fixture must reproduce quoted Git paths')
            result = root / 'paths.json'
            ps = root / 'git_paths.ps1'

            def quote(path):
                return str(path).replace("'", "''")

            ps.write_text(f"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{quote(SCRIPT)}',[ref]$tokens,[ref]$errors)
foreach ($name in @('Get-GitTrackedPaths','Assert-PlainPath','Write-Utf8')) {{
    $node=$ast.Find({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }},$true)
    . ([scriptblock]::Create($node.Extent.Text))
}}
# Reproduce the former exception on the actual C-quoted Chinese filename.
$quoted=@(& '{quote(git)}' -C '{quote(repo)}' -c core.quotepath=true ls-tree -r --name-only HEAD | Where-Object {{ $_.StartsWith('"') }})
$reproduced=$false
try {{ Assert-PlainPath (Join-Path '{quote(repo)}' $quoted[0]) }} catch {{ $reproduced=$true }}
if (-not $reproduced) {{ throw 'Original invalid-path error was not reproduced' }}
[Console]::OutputEncoding=[Text.Encoding]::GetEncoding(437)
$paths=@(Get-GitTrackedPaths '{quote(git)}' '{quote(repo)}' HEAD)
$remote=@(Get-GitTrackedPaths '{quote(git)}' '{quote(repo)}' origin/main)
if (@(Compare-Object $paths $remote).Count) {{ throw 'Revision listings differ' }}
foreach ($path in $paths) {{ Assert-PlainPath (Join-Path '{quote(repo)}' $path) }}
Write-Utf8 '{quote(result)}' (ConvertTo-Json -InputObject $paths)
$failed=$false
try {{ Get-GitTrackedPaths '{quote(git)}' '{quote(root)}' HEAD }} catch {{
    if ($_.Exception.Message -notlike '*Git file listing failed*') {{ throw }}
    $failed=$true
}}
if (-not $failed) {{ throw 'Non-repository must fail' }}
""", encoding='utf-8-sig')
            process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ps)],
                                     capture_output=True, text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(set(json.loads(result.read_text(encoding='utf-8'))), set(names))


@unittest.skipUnless(sys.platform == 'win32', 'Windows process containment')
class ProcessTreeTests(unittest.TestCase):
    def test_terminating_supervisor_also_terminates_child(self):
        source = embedded('runnerCode').split("if __name__ == '__main__':")[0]
        source += "\njob = contain_process_tree()\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\nprint(child.pid, flush=True)\nchild.wait()\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'containment.py'
            path.write_text(source, encoding='utf-8')
            process = subprocess.Popen([sys.executable, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            handle = None
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            try:
                line = process.stdout.readline().strip()
                self.assertTrue(line.isdigit(), process.stderr.read() if process.poll() is not None else line)
                handle = kernel.OpenProcess(0x100000, False, int(line))
                self.assertTrue(handle)
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                process.terminate()
                process.wait(timeout=10)
                self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0, 'Child survived supervisor termination')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if handle:
                    kernel.CloseHandle(handle)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
