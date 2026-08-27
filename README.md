# Encrypted Traffic Capture System

本项目把浏览器访问与 PCAP/TLS keylog 采集封装为 Worker Agent，并由主控 Flask
与 Vue 前端统一管理多台子机器。HTML、抓取报告和后处理均为显式可选项。

```text
Vue 前端 :5173 -> 主控 Flask :5200 -> Worker Agent :5100
                                             |-> 浏览器访问
                                             |-> TShark / PCAP
                                             `-> 可选 HTML / 报告 / 后处理
```

本文重点说明子机器 Worker 的完整部署顺序，并分别标记 Windows、Linux、macOS
的差异。主控和前端另见：

- [Worker API 说明](worker_agent/README.md)
- [主控 Flask 说明](master_server/README.md)
- [Vue 前端说明](frontend/README.md)
- [完整使用说明](使用说明.md)

## 1. Worker 固定约定

- 服务端口：`5100/TCP`
- API 前缀：`/api/v1`
- 支持浏览器：Chrome、Microsoft Edge、Firefox
- Safari：Worker 暂不开放，因为当前无法保证无缓存和 TLS 密钥导出
- 单台 Worker 同时只执行一个实验，其他任务进入队列
- HTTP 响应均包含 `Cache-Control: no-store`
- SQLite 只保存任务元数据，大型实验文件保存在本机任务目录
- 主控和 Worker 必须使用完全相同的 Worker Token

Worker 默认读取项目根目录的 `worker.yaml`。仓库只提交 `worker.yaml.example`，
真实配置包含 Token，已被 `.gitignore` 忽略。原有环境变量继续兼容，并且优先级高于
YAML，适合临时覆盖或由操作系统服务管理器注入。

主控采用相同方式，默认读取根目录的 `master.yaml`。首次使用时执行
`Copy-Item master.yaml.example master.yaml`，再按部署机器修改监听地址、数据目录、
跨域来源和可选的本机 Worker 自动注册配置。

## 2. 所有操作系统的共同前提

每台子机器需要：

1. Git 和 Python 3.10 或更高版本。
2. Chrome、Edge、Firefox 中至少一种。
3. Wireshark/TShark，以及允许当前用户抓包的系统权限。
4. 能访问 Python 包索引和浏览器驱动下载地址。
5. 如果主控和 Worker 不在同一局域网，两台机器应加入同一个 Tailscale 网络。

首次部署时克隆主分支：

```bash
git clone --branch main --single-branch \
  https://github.com/ThreeWater1037/Encrypted-Traffic-Capture-System.git
```

已有仓库时更新：

```bash
git switch main
git pull
```

建议为每台子机器使用唯一 ID，例如：

```text
win-worker-01
linux-worker-01
mac-worker-01
```

Token 应使用至少 32 字节的随机值，不要使用 `111`、`dev-worker-token` 等弱口令。

### 2.1 创建通用 YAML 配置

Windows：

```powershell
Copy-Item worker.yaml.example worker.yaml
notepad worker.yaml
```

Linux/macOS：

```bash
cp worker.yaml.example worker.yaml
${EDITOR:-vi} worker.yaml
```

核心内容如下：

```yaml
worker:
  id: win-worker-01
  host: 0.0.0.0
  port: 5100
  token: "替换为随机长Token"

paths:
  project_root: .
  python_executable:
  data_dir: ./worker_data
```

`python_executable` 留空时使用启动 Worker 的 Python。相对路径以 `worker.yaml`
所在目录为基准。需要把配置放在其他位置时设置 `WORKER_CONFIG_FILE`。

WSL/Linux 中的浏览器需要显式使用代理时，在同一文件中增加：

```yaml
network:
  proxy_url: http://<代理主机>:<端口>
```

该配置会同时注入 Chrome、Edge 和 Firefox；支持 `http`、`socks4`、`socks5`。

## 3. Windows 子机器

### 3.1 安装系统依赖

安装 Chrome、Edge 或 Firefox。通过 Wireshark 官方安装程序安装 Wireshark，并一并
安装 Npcap。项目会依次从环境变量、PATH、Windows 注册表和常见安装目录寻找浏览器。

验证常见安装位置：

```powershell
Test-Path 'C:\Program Files\Google\Chrome\Application\chrome.exe'
Test-Path 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
Test-Path 'C:\Program Files\Mozilla Firefox\firefox.exe'
Test-Path 'C:\Program Files\Wireshark\tshark.exe'
```

验证抓包接口：

```powershell
& 'C:\Program Files\Wireshark\tshark.exe' -D
```

如果 Wireshark 安装在其他位置，也可以直接运行对应的 `tshark.exe -D`。

### 3.2 创建 Python 环境并安装依赖

以下示例把虚拟环境放在项目目录之外：

```powershell
$ProjectRoot = 'D:\Project\Encrypted Traffic Capture System'
$VenvRoot = "$env:USERPROFILE\.venvs\encrypted-traffic-worker"

python -m venv $VenvRoot
$Python = "$VenvRoot\Scripts\python.exe"

Set-Location $ProjectRoot
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements-worker.txt
& $Python -m pip install selenium webdriver-manager
```

验证解释器和依赖：

```powershell
& $Python -c "import flask, waitress, selenium, webdriver_manager; print('dependencies=ok')"
& $Python wiki_fetcher.py --help
```

### 3.3 生成 Token

只生成一次并安全保存；主控注册该机器时填写相同 Token：

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$WorkerToken = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
$WorkerToken
```

### 3.4 写入 YAML 并启动

把第 3.3 节生成的 Token 写入 `worker.yaml`，同时按实际情况修改路径：

```yaml
worker:
  id: win-worker-01
  host: 0.0.0.0
  port: 5100
  token: "替换为已保存的随机Token"

paths:
  project_root: .
  python_executable: C:/Users/<用户名>/.venvs/encrypted-traffic-worker/Scripts/python.exe
  data_dir: D:/TrafficCaptureWorker/data
```

启动时不再逐项设置环境变量：

```powershell
$ProjectRoot = 'D:\Project\Encrypted Traffic Capture System'
$Python = "$env:USERPROFILE\.venvs\encrypted-traffic-worker\Scripts\python.exe"
Set-Location $ProjectRoot
& $Python -m worker_agent
```

安装了 Waitress 后会自动使用 Waitress。首次调试建议保持窗口打开，以便直接查看错误。

### 3.5 Windows 特有说明

- `PYTHON_EXECUTABLE` 应指向安装了 Selenium 和 webdriver-manager 的解释器。
- Chrome TLS keylog 使用临时 ASCII 路径，程序结束后再复制回实验目录。
- 如果抓包失败，确认 Npcap 已安装，并尝试用管理员 PowerShell 启动 Worker。
- 特殊浏览器路径可以通过 `CHROME_BINARY`、`EDGE_BINARY`、`FIREFOX_BINARY` 覆盖。
- 防火墙只应允许主控 IP 访问 `5100/TCP`。

管理员 PowerShell 防火墙示例：

```powershell
New-NetFirewallRule `
  -DisplayName 'FlowLab Worker 5100' `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 5100 `
  -RemoteAddress '<主控电脑IP或Tailscale-IP>'
```

## 4. Linux 子机器

以下命令以 Ubuntu/Debian 和 Bash 为例；其他发行版请替换包管理器命令。

### 4.1 安装系统依赖

```bash
sudo apt update
sudo apt install -y git python3 python3-venv tshark
```

安装 Chrome、Edge 或 Firefox 中至少一种，然后检查：

```bash
command -v google-chrome || command -v chromium || command -v chromium-browser
command -v microsoft-edge || command -v microsoft-edge-stable
command -v firefox
command -v tshark
tshark -D
```

Ubuntu/Debian 非 root 抓包通常还需要配置 Wireshark 权限：

```bash
sudo dpkg-reconfigure wireshark-common
sudo usermod -aG wireshark "$USER"
```

完成后注销并重新登录，再运行 `tshark -D`。不同发行版的 `dumpcap` 权限和用户组
名称可能不同，应遵循该发行版的 Wireshark 软件包说明。

### 4.2 创建环境并安装依赖

```bash
PROJECT_ROOT="$HOME/Encrypted-Traffic-Capture-System"
VENV_ROOT="$HOME/.venvs/encrypted-traffic-worker"

python3 -m venv "$VENV_ROOT"
PYTHON="$VENV_ROOT/bin/python"

cd "$PROJECT_ROOT"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements-worker.txt
"$PYTHON" -m pip install selenium webdriver-manager

"$PYTHON" -c "import flask, waitress, selenium, webdriver_manager; print('dependencies=ok')"
"$PYTHON" wiki_fetcher.py --help
```

### 4.3 写入 YAML 并启动

先生成并保存 Token：

```bash
WORKER_TOKEN_VALUE="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
printf '%s\n' "$WORKER_TOKEN_VALUE"
```

把 Token 写入 `worker.yaml`，将机器 ID 和数据目录改为实际值：

```yaml
worker:
  id: linux-worker-01
  host: 0.0.0.0
  port: 5100
  token: "替换为已保存的随机Token"

paths:
  project_root: .
  python_executable: ~/.venvs/encrypted-traffic-worker/bin/python
  data_dir: ~/traffic-capture-worker/data
```

启动：

```bash
PROJECT_ROOT="$HOME/Encrypted-Traffic-Capture-System"
PYTHON="$HOME/.venvs/encrypted-traffic-worker/bin/python"

cd "$PROJECT_ROOT"
"$PYTHON" -m worker_agent
```

### 4.4 Linux 特有说明

- 浏览器通常从 PATH 或 `/usr/bin` 自动发现。
- Linux/WSL 抓包固定使用 TShark 的 `any` 接口，覆盖全部实体接口并避开
  `nflog`、DBus、蓝牙监控等不可用伪接口。
- PCAP 权限应通过发行版提供的 `dumpcap` 权限或 `wireshark` 用户组配置，不建议长期以 root 运行整个 Worker。
- 无桌面服务器需要额外提供可运行的浏览器环境；当前实现不是纯 HTTP 抓取器。
- 使用 UFW 时可只允许主控访问：

```bash
sudo ufw allow from '<主控IP或Tailscale-IP>' to any port 5100 proto tcp
```

## 5. macOS 子机器

### 5.1 安装系统依赖

安装 Git、Python 3，以及 Chrome、Edge、Firefox 中至少一种。安装 Wireshark 官方
macOS 包后，执行安装镜像中的 `Install ChmodBPF.pkg`，使非 root 用户具备抓包权限。

验证：

```bash
command -v python3
test -x '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
test -x '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'
test -x '/Applications/Firefox.app/Contents/MacOS/firefox'
test -x '/Applications/Wireshark.app/Contents/MacOS/tshark'
'/Applications/Wireshark.app/Contents/MacOS/tshark' -D
```

项目能够自动识别这些 `/Applications` 路径。

### 5.2 创建环境并安装依赖

```bash
PROJECT_ROOT="$HOME/Encrypted-Traffic-Capture-System"
VENV_ROOT="$HOME/.venvs/encrypted-traffic-worker"

python3 -m venv "$VENV_ROOT"
PYTHON="$VENV_ROOT/bin/python"

cd "$PROJECT_ROOT"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements-worker.txt
"$PYTHON" -m pip install selenium webdriver-manager

"$PYTHON" -c "import flask, waitress, selenium, webdriver_manager; print('dependencies=ok')"
"$PYTHON" wiki_fetcher.py --help
```

### 5.3 写入 YAML 并启动

```bash
WORKER_TOKEN_VALUE="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
printf '%s\n' "$WORKER_TOKEN_VALUE"
```

保存 Token 后写入 `worker.yaml`：

```yaml
worker:
  id: mac-worker-01
  host: 0.0.0.0
  port: 5100
  token: "替换为已保存的随机Token"

paths:
  project_root: .
  python_executable: ~/.venvs/encrypted-traffic-worker/bin/python
  data_dir: ~/traffic-capture-worker/data
```

启动：

```bash
PROJECT_ROOT="$HOME/Encrypted-Traffic-Capture-System"
PYTHON="$HOME/.venvs/encrypted-traffic-worker/bin/python"

cd "$PROJECT_ROOT"
"$PYTHON" -m worker_agent
```

### 5.4 macOS 特有说明

- Worker 只开放 Chrome、Edge、Firefox，不能选择 Safari。
- Safari 底层可被 Selenium 驱动，但无法提供本项目要求的 TLS keylog 和等价无缓存保证。
- 如果 macOS 防火墙询问是否允许 Python/Waitress 接收连接，应仅在可信网络中允许。
- TShark 常见路径为 `/Applications/Wireshark.app/Contents/MacOS/tshark`。

## 6. 通用验证步骤

### 6.1 检查端口

Windows：

```powershell
netstat -ano | findstr LISTENING | findstr :5100
```

Linux/macOS：

```bash
lsof -nP -iTCP:5100 -sTCP:LISTEN
```

### 6.2 健康检查

Windows：

```powershell
Invoke-RestMethod 'http://127.0.0.1:5100/api/v1/health'
```

Linux/macOS：

```bash
curl --fail --show-error 'http://127.0.0.1:5100/api/v1/health'
```

返回结果应包含：

```json
{
  "status": "ok",
  "busy": false,
  "cache_enabled": false
}
```

### 6.3 能力检查

Windows：

```powershell
$headers = @{ Authorization = 'Bearer <Worker Token>' }
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:5100/api/v1/capabilities' `
  -Headers $headers
```

Linux/macOS：

```bash
curl --fail --show-error \
  -H 'Authorization: Bearer <Worker Token>' \
  'http://127.0.0.1:5100/api/v1/capabilities'
```

重点确认：

- `browsers` 中至少有一个浏览器。
- `capture.pcap` 为 `true`。
- `capture.tshark_path` 指向真实文件。
- 只有需要后处理时才要求 `analysis.scripts_ready` 为 `true`。

## 7. 跨网络连接

主控与 Worker 不在同一局域网时，推荐两台机器加入同一个 Tailscale 网络，不要把
`5100` 直接映射到公网。

查看 Worker 的 Tailscale IP：

```bash
tailscale ip -4
```

在主控机器验证：

```bash
tailscale ping <Worker-Tailscale-IP>
curl --noproxy '*' "http://<Worker-Tailscale-IP>:5100/api/v1/health"
```

如果主控机器使用 HTTP 代理，启动主控前应让 Worker IP 绕过代理：

Windows PowerShell：

```powershell
$env:NO_PROXY='localhost,127.0.0.1,<Worker-Tailscale-IP>'
$env:no_proxy=$env:NO_PROXY
```

Linux/macOS：

```bash
export NO_PROXY="localhost,127.0.0.1,<Worker-Tailscale-IP>"
export no_proxy="$NO_PROXY"
```

主控“机器管理”填写：

```text
机器 ID：与 WORKER_ID 对应的唯一名称
Worker 地址：http://<Worker-Tailscale-IP>:5100
Worker Token：与 WORKER_TOKEN 完全相同
启用：是
```

保存后点击“重新探测”，状态应变为 `ONLINE`。

## 8. 数据位置

Worker SQLite：

```text
WORKER_DATA_DIR/worker.db
```

任务文件：

```text
WORKER_DATA_DIR/tasks/<task_id>/
|-- request.json
|-- input.tsv
|-- worker.log
|-- manifest.json
`-- fetch_output/
```

默认任务只把 PCAP 与 TLS keylog 作为实验产物保存和校验。页面 HTML、逐 URL/批次
报告，以及 `batch_process.py` 生成的 TSV、`capture_*_flows/`、
`capture_*_inferred/` 等后处理产物，只有在创建任务时显式启用才会生成。

## 9. 大批量、断点续跑与 24 小时运行

- 单个任务默认最多 `100000` 个 URL，请求体上限为 `256 MiB`；Worker 的总任务
  超时默认为 `0`，表示不因运行数天而主动终止任务。
- 每个“URL + 浏览器”只有在 TLS keylog、PCAP（启用时）及其他显式要求的产物
  均非空后，才原子写入 `capture_<browser>.complete.json`。进程中断时，当前半截
  URL 不会被当作成功；服务恢复后会重抓当前单元，已提交检查点的单元会跳过。
- `capture_progress.json` 记录最近处理位置，实际续跑判据始终是逐单元完成标记，
  因此即使最后一个 URL 在写文件过程中断电，也不会误跳过损坏的 PCAP。
- 抓取脚本非零退出时，Worker 每 5 秒自动重启并从检查点继续；Worker 重启会把
  未完成任务重新排队；Worker 暂时离线时，主控保持任务为
  `WAITING_FOR_WORKER`，不会因一次网络波动直接判失败。
- 代码恢复不能代替进程保活。服务器部署必须用 systemd、Windows 服务管理器或
  容器的 restart policy 同时托管 Master 和 Worker；程序收到 `SIGTERM` 时会先
  终止当前抓包子进程并保留可恢复状态。

百万 URL 不建议作为一个请求直接提交：当前代码层面的单任务上限是 10 万。
百万级应按稳定、不可变的输入分片（建议每片 1 万至 5 万），每片使用独立
`job_id`，并先完成目标服务器的磁盘容量估算和至少一轮同规模长稳测试。

## 10. 常见问题

### 端口被占用

不要重复启动 Worker。先根据第 6.1 节找到旧进程，再决定是否停止。

### `ModuleNotFoundError: selenium`

说明 `PYTHON_EXECUTABLE` 指向的解释器没有安装采集依赖：

```bash
<PYTHON_EXECUTABLE> -m pip install selenium webdriver-manager
```

### `401 Worker Token 无效`

主控保存的 Token 与 Worker 启动时的 `WORKER_TOKEN` 不一致。

### `502`、连接被拒绝或超时

依次检查 Worker 是否监听 `0.0.0.0:5100`、系统防火墙、Tailscale 状态和主控代理。
先在主控机器直接请求 Worker 的 `/health`，再排查主控 Flask。

### 浏览器没有出现在能力列表

先确认浏览器真实存在且当前用户可执行。特殊安装位置使用：

```text
CHROME_BINARY=<绝对路径>
EDGE_BINARY=<绝对路径>
FIREFOX_BINARY=<绝对路径>
```

### PCAP、TLS keylog 或可选后处理产物缺失

先运行 `tshark -D` 确认抓包权限，并检查浏览器是否实际写出了 `tls_keys_*.log`。
注意 `capture_*_flows/` 和
`capture_*_inferred/` 只有在 `batch_process.py` 对抓包结果完成后处理后才会出现。

## 11. 官方参考

- [Python venv 文档](https://docs.python.org/3/library/venv.html)
- [Wireshark 安装文档](https://www.wireshark.org/docs/wsug_html_chunked/ChapterBuildInstall.html)
- [Selenium 支持的浏览器](https://www.selenium.dev/documentation/webdriver/browsers/)
- [Tailscale 安装文档](https://tailscale.com/docs/install)
