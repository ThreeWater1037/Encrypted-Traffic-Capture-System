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

新服务器可直接使用 [Windows 一键部署脚本](deployment/deploy_worker_windows_README.md)，自动安装 Git、Miniconda、浏览器及抓包环境，并配置开机运行；首次 Npcap 普通版安装需完成向导。下面保留手工部署步骤。

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

Chrome、Edge、Firefox 默认按 **目标页面响应完成** 判定采集成功。三种浏览器使用
`eager` 导航（DOM 就绪即可返回），再通过 CDP / BiDi 的共同请求账本确认主页面最终
HTTP 2xx 响应已完整接收，且不是本地缓存响应。主页面失败、HTTP 4xx/5xx、未完成或
缺少有效网络观测仍失败，不以文件存在或固定等待替代验证。

导航与主页面响应共用 **30 秒**预算。主页面完成后继续观察附带资源：全部请求结束并
连续 **500 毫秒**静默即可结束；否则连续 **3 秒无网络进展**，或自主页面响应完成起
达到 **10 秒**资源预算，就停止等待。广告、统计、图片、iframe、XHR/fetch 的未完成
请求保留在诊断中，不再单独阻止目标 URL 成功。资源可能未采完整，结果不等于整页
所有资源完成；WebSocket/EventSource 长连接继续不阻塞结束。

`network_status_<browser>.json` 与完成检查点的 `network_summary` 保存
`completion_policy=target_document`、`completion_reason`、`target_document`、
`network_complete`、`resource_status`、未完成请求及警告。附带资源不完整时仍可提交
成功检查点，`resource_status=partial`，不会因为该警告自动重复采集。

启动抓包时不再固定等待 1.2 秒。TShark 的本次输出文件初始化通知、完整的 PCAPNG
文件头及所选接口描述都就绪后立即启动浏览器，等待上限为 5 秒。stderr 持续转发到
Worker 日志；接口错误、提前退出或就绪超时会失败，不会生成成功检查点。tcpdump
使用独立的监听通知及 PCAP 文件头校验。

三种浏览器读取元数据后复查同一个请求账本，复用原有资源预算，不重新计时。
已去掉页面后和抓包停止前各 0.5 秒的重复等待。停止时等待进程正常刷新并退出，再流式检查
PCAP/PCAPNG 结构、非空数据包与截断情况；强制终止、非零退出码或损坏文件均失败。
文件结构校验不能替代解密后的 HTTP 响应完整性审计。

Chrome/Edge/Firefox 驱动服务使用有界进程退出等待，避免轮询已关闭的 HTTP 状态端口。
新建 WebDriver 会话的 HTTP 等待为 30 秒，导航命令为 35 秒（给页面 30 秒预算留出返回余量），
普通命令为 10 秒，退出命令为 5 秒，并关闭本机驱动 HTTP 自动重试；这些是各阶段预算，
不代表整个采集单元总耗时只有 30 秒，驱动解析、初始化和收尾另计。
浏览器退出、驱动服务停止、TLS 密钥复制及临时配置清理分别计时；失败保留诊断，
不会默默继续提交成功。完成检查点和网络状态文件还包含 `capture_summary` /
`cleanup_summary`。浏览器仍按 URL 独立启动。

每个 URL 仍启动独立浏览器和全新配置目录；Chrome/Edge 启动及导航前通过 CDP 禁用
HTTP 缓存并显式绕过 Service Worker。独立浏览器 CDP 连接在新的跨进程 iframe、
Worker 等目标运行前递归应用相同策略，保留浏览器站点隔离；其网络完成事件也用于
等待判断。策略初始化失败或目标主页面命中缓存 / 304 时，本次抓取失败；附带资源的
缓存命中保留为诊断警告，不再使整个目标失败，细节写入
`network_summary.cache_policy` / `cache_hit_requests`。Firefox 使用 WebDriver BiDi
在整个会话禁用 HTTP 缓存，并核验缓存响应和 304；在全新 Profile 中禁用 Service Worker
注册，避免其缓存或合成响应。这与 Chromium 的绕过方式不同，依赖 Service Worker 的
页面行为可能变化，策略会明确记录在诊断文件中。BiDi 初始化失败直接报错，不退回固定等待。
Firefox 允许同一页面文档内复用本次已完整下载的图片：必须先观测到该图片的 GET 请求
以非缓存 HTTP 200 完整结束，再允许同一文档、同一 URL 的图片缓存响应。原始缓存标记
保留，并在 `same_document_image_reuses` 中记录首次下载的请求 ID。首次加载就命中缓存、
首次下载未完成或失败、304、非图片缓存仍记录为缓存异常；目标主页面的缓存异常仍失败。
导航、重新加载、观测重置或下一 URL
采集不会继承放行依据；每个 URL 仍使用独立浏览器与全新 Profile。Chrome/Edge 维持
原来的缓存及 Service Worker 绕过策略。
三种浏览器均保存 `network_status_<browser>.json`。旧页面的缓存、Cookie
和 Service Worker 注册不会继承到下一 URL。不再全局注入 Cache-Control/Pragma，
避免给跨域资源引入不被允许的预检请求。前一 URL 的抓包关闭、浏览器退出及临时
配置清理全部完成后，默认等待 **1 秒**再访问下一项。命令行可覆盖：

```powershell
python wiki_fetcher.py --input urls.txt --browsers edge --pcap --network-idle-seconds 0.5 --interval-seconds 1
```

将 `--interval-seconds` 设为 `3` 可恢复原批次间隔；`--network-idle-seconds 2` 保留
两秒静默窗口，但仍受附带资源 3 秒停滞 / 10 秒总预算约束。Chrome/Edge/Firefox
共用完成策略；Safari 仍使用 complete 后的固定延时。超过附带资源预算的未完成请求、
更晚才由定时器发起的请求不保证采集完整。

Chrome/Edge/Firefox 采集 `forbeschina.com` 及其子域名时，在导航前精确屏蔽
`https://www.google.com/recaptcha/api2/aframe`（仅此完整 URL，不匹配其他路径或带查询参数的 URL）。
同时屏蔽 `https://www.google-analytics.com/g/collect`，该规则允许任意查询参数，
但不匹配其他主机、`/g/collect-other` 或 `/g/collect/extra`。
Chrome/Edge 使用 CDP，并覆盖后续创建的子页面和 Worker；Firefox 使用 BiDi 拦截。
规则按输入页面域名启用，不对其他站点全局屏蔽。结果属于主动排除该组件后的流量。
`network_status_<browser>.json` 的 `request_blocking` 记录规则；请求账本保留原始失败事件，
并通过 `policy_reason=forbes_recaptcha_exclusion` / `forbes_analytics_exclusion`
和 `intentionally_blocked_requests` 标识主动屏蔽。其他附带请求使用上述有界等待策略。
Firefox 临时 Profile 禁用 Service Worker、Chrome/Edge 绕过 Service Worker 的策略不变。

Chrome/Edge/Firefox 访问 `today.hit.edu.cn` 时采用相同的旧站屏蔽策略：
保留 `today2.hit.edu.cn`、`myweb.hit.edu.cn` 的 HTTP/HTTPS 资源隔离；
其余资源使用统一目标页面完成策略；附带资源达到 3 秒停滞 / 10 秒预算后保留
未完成请求诊断，主页面完整响应仍可成功。

受影响页面会保留可供补抓的标记，即使没有启用 HTML/报告输出：

- 每个 URL 目录的 `resource_status_<browser>.json` 保存最近一次结果，包含页面 URL、
  `needs_recapture`、`resource_status`、被跳过资源的完整 URL、原因和时间。
- 输出根目录的 `pages_needing_recapture.jsonl` 追加记录所有需补抓的尝试，包含产物目录
  和 `run_id`；这是历史清单，可能有同一页面的多次记录，最新状态以逐 URL 文件为准。
- 完成检查点也包含 `skipped_resources`、`needs_recapture` 和 `resource_status`。
  `partial` 表示正文采集完成但有资源缺失，批次可以继续；不代表图片全部加载成功。
  `isolated_legacy_host` 表示旧域名隔离。

以上资源标记适用于三种受支持浏览器的今日哈工大页面。已有产物不会被追溯修改。
后续采集改动必须同步检查三种浏览器，维护要求见 [AGENTS.md](AGENTS.md)。

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
