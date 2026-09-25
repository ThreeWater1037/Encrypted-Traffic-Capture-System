# Windows Worker 一键部署与更新

脚本：[deploy_worker_windows.ps1](deploy_worker_windows.ps1)。只需将这一个文件复制到服务器，脚本会从 Gitee `main` 克隆项目。适用于 **Windows Server 2019/2022/2025 桌面体验版、Windows 10 1809+ / Windows 11，x64 架构**；需要管理员权限。不支持 Server Core、ARM64 或 Windows Server 2016 及更早版本。

自动安装或复用 Git、Miniconda、Python 3.12、Worker 依赖、Selenium、webdriver-manager、Chrome、Edge、Firefox、Wireshark/TShark；生成配置与 Token，配置开机自动运行，再执行三种浏览器的真实 HTTPS 抓包验收。无需预装 Python、Git、Conda、winget 或 Node.js。

Worker 计划任务使用**执行部署的 Windows 管理员账号**。部署时会提示输入这个账号的 Windows 登录密码（不是 PIN），由 Windows 计划任务保存凭据，实现注销后运行及重启自启。脚本不会将明文密码写入配置或日志；也不创建新账号。不能使用 SYSTEM 运行 Edge，参见 [Microsoft Edge 的说明](https://learn.microsoft.com/en-us/answers/questions/1537064/run-microsoft-edge-with-system-account)。

## 默认下载源（国内镜像优先）

无需额外参数，默认使用下表的下载源；**并非所有组件都有已验证的国内镜像**。

| 资源 | 默认来源 |
|---|---|
| 项目代码 | Gitee `main` |
| Git for Windows | npmmirror，按版本号选择正式版，排除 RC |
| Miniconda | 南京大学镜像优先、北京大学备用；固定 `py312_26.7.1-1`，核对 Anaconda 官方 SHA-256 |
| Conda Python 环境 | [上海交大 conda-forge 镜像](https://mirror.sjtu.edu.cn/docs/anaconda)，已核查包含 Windows Python 3.12 包 |
| pip 依赖 | 阿里云 PyPI 镜像，所有 pip install 显式指定源 |
| Chrome | npmmirror 的 **Chrome for Testing 154.0.8037.57** 完整 ZIP，固定版本及与 Google 官方完整包一致的 SHA-256；已有系统 Chrome 则复用 |
| Firefox | npmmirror 的正式版 x64 中文 MSI，排除 Beta、ESR 别名 |
| ChromeDriver / EdgeDriver / GeckoDriver | npmmirror；部署时下载至本地并写入计划任务环境，采集时无需再访问上游解析驱动 |
| Edge 安装包 | Microsoft 官方企业 CDN；尚未找到可靠的国内镜像，可预置 `edge.msi` |
| Wireshark | [南京大学镜像](https://mirrors.nju.edu.cn/wireshark/win64/)优先、[阿里云镜像](https://mirrors.aliyun.com/wireshark/win64/)备用；固定稳定版 `4.6.9`，校验官方 SHA-256 |
| Npcap | 官方源，可预置 `npcap.exe`；向导要求保持不变 |
| 默认验收网站 | `https://www.baidu.com/robots.txt`，可通过 `-SmokeUrl` 更换 |

已完整下载并核验上述安装包，而不只是检查 HTTP 状态或文件头。实际访问能力仍取决于服务器网络。缺失组件下载失败时明确报错，不会偷偷切回 GitHub/Google 下载。交大 Miniconda 的 `latest` 链接本次实际返回 2020 年的 `py38_4.9.2`，因此不再使用该安装包链接；其 conda-forge 仓库已单独核查包含 Python 3.12。

Chrome for Testing 是完整的自动化测试版 Chrome，它不会自行更新。该版本已用本项目相同的启动参数完成本地 Selenium 页面访问测试。已有 Chrome/Edge 若自动更新，需重新运行部署脚本匹配驱动。ChromeDriver 与浏览器匹配前三段版本，EdgeDriver 默认下载与 Edge 完全相同的版本；镜像尚未同步时可预置匹配的驱动。GeckoDriver 默认固定为已核查存在的 `0.37.1`，可配置版本。

## 第一次执行

1. 将 `deploy_worker_windows.ps1` 放到服务器的 `C:\Deploy\` 等目录，**不要放在将被部署的 `C:\TrafficWorker` 内**。
2. 通过远程桌面登录，在开始菜单右键 **Windows PowerShell → 以管理员身份运行**，使用 64 位版本。
3. 执行以下命令，每台服务器换一个 Worker ID：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -WorkerId win-worker-01 -InteractiveNpcap
```

**首次安装 Npcap 时会弹出向导，需要手动完成这一步。** [Wireshark 静默安装不会安装 Npcap](https://www.wireshark.org/docs/wsug_html_chunked/ChBuildInstallWinInstall.html)，而 [Npcap 的静默安装只在 OEM 版提供](https://npcap.com/guide/npcap-users-guide.html)。`-InteractiveNpcap` 表示允许打开该向导；已有 Npcap 时会直接复用，不弹窗。如果驱动或其他安装器提示重启，请重启服务器后重新执行同一条命令。脚本不会自行重启服务器。

若已经有合法获得的 Npcap OEM 安装包，可完全无人值守部署：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -WorkerId win-worker-02 -NpcapOemInstaller C:\Deploy\npcap-oem.exe
```

已有 Npcap 的服务器可以省略上述两个 Npcap 参数。未安装且未指定参数时，脚本明确停止并提示，重新运行会复用已安装组件。

## 安装位置与运行方式

| 内容 | 默认位置 / 设置 |
|---|---|
| 独立部署目录 | `C:\TrafficWorker`，仅管理员和 SYSTEM 可访问 |
| 项目代码 | `C:\TrafficWorker\project` |
| Miniconda | `C:\TrafficWorker\miniconda` |
| Worker Python | `C:\TrafficWorker\miniconda\envs\traffic-worker\python.exe` |
| 专用 Chrome for Testing | `C:\TrafficWorker\browsers\chrome-win64\chrome.exe` |
| 本地浏览器驱动 | `C:\TrafficWorker\drivers` |
| 配置与 Token | `C:\TrafficWorker\project\worker.yaml` |
| 任务数据 | `C:\TrafficWorker\project\worker_data` |
| 开机任务 | 任务计划程序根目录中的 `TrafficCaptureWorker` |
| 运行身份 | 执行部署的 Windows 管理员账号，Password 登录类型、最高权限；凭据由 Windows 计划任务保存，无需保持 RDP 登录 |
| 部署日志 | `C:\TrafficWorker\logs\deploy-时间戳.log` |
| Worker 日志 | `C:\TrafficWorker\logs\worker.stdout.log`、`worker.stderr.log` |
| 配置、任务定义备份 | `C:\TrafficWorker\backups\时间戳` |
| 监听端口 | `0.0.0.0:5100` |

这是 Windows **计划任务**，不是 `services.msc` 中的 Windows 服务。以 headless 模式运行浏览器，退出远程桌面后继续工作。异常退出一分钟后重试，最多连续重试 999 次。监督进程使用 Windows Job Object，在停止计划任务时同时结束其 Worker、浏览器、驱动和 TShark 子进程。日志单文件超过 20 MB 时在下次启动轮换一次；连续运行期间不自动截断日志。

可用 `-InstallRoot D:\TrafficWorker` 更换盘符或目录，后续更新必须继续指定同一个目录。请使用新的专用目录：脚本拒绝接管非空且没有本脚本标记的目录，也不会迁移手工安装的旧 Worker。全机只管理一个名为 `TrafficCaptureWorker` 的任务。

Edge、Firefox 采用系统级安装；Chrome 优先复用系统版，否则安装专用 Chrome for Testing。每次采集使用独立临时浏览器 Profile。浏览器网页代理可在 `worker.yaml` 的 `network.proxy_url` 中设置；下载源需允许服务器访问。新 Python 环境通过镜像站的 conda-forge 创建，不修改系统默认 Python，也不修改管理员的 PowerShell Profile 或全局 pip 配置。

每次部署/修复会再次要求启动账号凭据；也可从调用脚本中传入 `-TaskCredential`（PSCredential 对象）。只接受当前提权 PowerShell 的同一个管理员账号，以匹配部署目录权限。账号密码变更后，需要用新密码重新注册任务。这里使用 Password 登录类型，不使用只能在登录会话中运行的 Interactive 类型，也不使用受网络凭据限制的 S4U 类型。

## 接入 Master 与防火墙

已知 Master 的实际出口 IPv4 时，在部署命令末尾追加：

```powershell
-MasterIP 192.0.2.10
```

将示例 IP 替换为真实地址。脚本只创建/更新自己的 Windows 防火墙规则，允许该 IP 访问 TCP 5100；不指定时不改防火墙。它不会修改其他已有规则、防火墙启用状态或云安全组。云安全组也需放行 **Master 来源 IP → TCP 5100**。

在 Master 添加 Worker：

- 地址：`http://服务器IP:5100`，不要附加 `/api/v1`。
- Token：在服务器本地打开 `C:\TrafficWorker\project\worker.yaml` 获取，部署日志不打印 Token。
- 最后从前端下发一个小任务，确认 Master → Worker 的完整链路；脚本自行创建的验收任务不会出现在 Master 任务列表。

## 后续更新

先将项目改动提交并推送到 Gitee `main`，再把最新部署脚本复制到服务器的 `C:\Deploy\`，执行同一命令。

- 复用已有系统浏览器、Git、Conda 或 Npcap；专用 Chrome for Testing 每次从校验后的缓存 ZIP 重新解压，修复中断造成的缺失文件，并与脚本指定的版本一致。
- 无效或过期的安装包缓存改名保留为 `.rejected-*` 后重新下载；未成功创建 `conda.exe` 或环境 `python.exe` 的半成品目录移入本次备份目录后重建，不要求手工删除整个部署目录。
- 仅允许 `main` 分支、origin 匹配、已跟踪文件无修改且能够快进的 Git 更新。不执行 `reset --hard` 或 `clean`。
- 保留已有 Worker ID、Token、数据目录、代理、CORS 和任务限制；显式 `-WorkerId`、`-RotateToken` 可覆盖对应设置。默认示例 Token 会替换为随机 Token。
- 更新项目/Python/浏览器路径，固定监听 `0.0.0.0:5100`。启动进程清除可能覆盖 YAML 的旧 Worker 环境变量。
- 备份配置、计划任务定义、运行脚本及前后提交号；不备份整套 Conda、系统软件和完整抓包数据。
- 有正在执行/排队任务时默认拒绝更新。部署前应暂停 Master 下发新任务，检查和停止不是原子操作。
- 停止计划任务后才更改代码和依赖；失败不自动回滚，计划任务可能保持停止/禁用状态。修复原因后重新执行脚本。

常用参数示例：

```powershell
# 选择服务器能访问的 HTTPS 测试网站，允许最多 20 分钟验收
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -SmokeUrl https://www.baidu.com/robots.txt -SmokeTimeout 1200

# 只部署并检查 API，不作真实抓包验收
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -SkipSmoke

# 明确允许中断当前任务，Worker 下次启动后按检查点恢复
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -AllowInterrupt

# 更换 Token，之后同步修改 Master 中保存的 Token
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -RotateToken
```

## 下载受限时预先上传安装包

在能正常下载的电脑上获取 x64 安装包，再通过 RDP 复制到服务器 `C:\Deploy\installers`，按以下名称保存，并追加 `-InstallerDirectory C:\Deploy\installers`。仅对缺失组件使用安装包；不要求所有包都存在，缺少的包仍按上表下载。

| 文件名 | 安装包来源 |
|---|---|
| `git.exe` | [Git for Windows](https://gitforwindows.org/) 完整安装包 |
| `miniconda.exe` | [Miniconda Windows x86_64](https://repo.anaconda.com/miniconda/)，默认必须为 `Miniconda3-py312_26.7.1-1-Windows-x86_64.exe` |
| `chrome.msi` | [Chrome Enterprise](https://chromeenterprise.google/download/) Stable x64 MSI |
| `chrome-win64.zip` | npmmirror 的 Chrome for Testing `154.0.8037.57` 完整浏览器包；与 `chrome.msi` 二选一 |
| `edge.msi` | [Edge for Business](https://www.microsoft.com/edge/business/download) Stable Windows x64 MSI |
| `firefox.msi` | [Firefox](https://www.mozilla.org/firefox/all/) Windows x64 MSI |
| `wireshark.exe` | [Wireshark](https://www.wireshark.org/download.html) `4.6.9` Windows x64 完整安装包 |
| `npcap.exe` | [Npcap](https://npcap.com/) 普通安装包，需要 `-InteractiveNpcap` |
| `chromedriver.exe` / `msedgedriver.exe` / `geckodriver.exe` | 匹配版本的 Windows 驱动，直接放在此目录即可，优先于联网解析 |

OEM Npcap 通过 `-NpcapOemInstaller` 单独传入。Git、Edge、Firefox、Npcap 和预置 Chrome MSI 检查 Windows Authenticode 签名；失败时输出状态及 Windows 的详细原因。Wireshark 使用固定的[官方 SHA-256](https://www.wireshark.org/download/SIGNATURES-4.6.9.txt)，与 Miniconda 采用相同的哈希及签名诊断流程。

Miniconda 使用脚本内固定的 [Anaconda 官方 SHA-256](https://repo.anaconda.com/miniconda/)。即使服务器的 Authenticode 返回 `UnknownError`，也只有**文件哈希与固定的官方值完全一致**才允许安装；哈希不符直接拒绝。Chrome for Testing 的该版 `chrome.exe` 没有 Authenticode 签名，因此校验完整 ZIP 的固定 SHA-256；该值由 [Google 官方下载源](https://googlechromelabs.github.io/chrome-for-testing/) 的完整包计算，并与 npmmirror 完整包交叉核对。

| 固定包 | SHA-256 |
|---|---|
| Miniconda `py312_26.7.1-1` Windows x64 | `8ae918681b0830314d85207f7a244352762b543bb83d53e0fcf77fbf270f8331` |
| Chrome for Testing `154.0.8037.57` win64 ZIP | `676f51fb82608330db5510ffba53d9e2762d3d7a99464afce54f9e9e25ad6bf7` |
| Wireshark `4.6.9` Windows x64 | `bf9b5ce8a89f244c376a9b1a946276eaa06463dde3e33069a34d7f102f5878cf` |

驱动 ZIP 检查压缩内容可读取、解压路径、目标可执行文件及版本（并非所有驱动都有 Authenticode 签名）。所有下载使用 HTTPS。每个下载源最多尝试三次，Miniconda 和 Wireshark 首选源失败后立即尝试备用源；缓存损坏会自动隔离并重新下载。用户通过 `-InstallerDirectory` 显式提供的包如果验证失败会停止，不会覆盖有效缓存。不要关闭 TLS 校验或把错误包的实测哈希填成可信值。

这只是安装包预置，不是完整离线部署：Gitee、Conda/pip 镜像和测试网站仍需可访问。脚本会将三个驱动的绝对路径写入 `run\runtime.json` 并传入 Worker，避免依赖计划任务账号的 PATH 或 webdriver-manager 在线元数据。

例如，已上传 Edge、Wireshark、Npcap 安装包时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -WorkerId win-worker-01 -InteractiveNpcap -InstallerDirectory C:\Deploy\installers
```

## 自定义所有下载地址

有单位内部镜像或其他可访问的 HTTPS 下载地址时，保存为 UTF-8 JSON，例如 `C:\Deploy\download-sources.json`：

```json
{
  "MinicondaUrl": "https://mirrors.nju.edu.cn/anaconda/miniconda/Miniconda3-py312_26.7.1-1-Windows-x86_64.exe",
  "MinicondaSha256": "8ae918681b0830314d85207f7a244352762b543bb83d53e0fcf77fbf270f8331",
  "CondaChannel": "https://mirror.sjtu.edu.cn/anaconda/cloud/conda-forge",
  "PipIndexUrl": "https://mirrors.aliyun.com/pypi/simple/"
}
```

执行时追加 `-DownloadConfig C:\Deploy\download-sources.json`。未写的字段保留默认值。可设置字段：

| 字段 | 含义 |
|---|---|
| `BinaryMirror` | 与 npmmirror 二进制 API 格式兼容的根地址，用于版本目录和包下载 |
| `MinicondaUrl`、`MinicondaFallbackUrl` | Miniconda 安装包和备用源，必须提供同一个版本的文件；自定义主 URL 时清除默认备用源，需自行明确配置备用源 |
| `MinicondaSha256` | Miniconda 上游可信 SHA-256；更换版本时同时更新 |
| `WiresharkFallbackUrl`、`WiresharkSha256` | Wireshark 同版本备用下载地址及官方 SHA-256；自定义 `WiresharkUrl` 时清除默认备用源，更换版本时同时更新哈希 |
| `CondaChannel`、`PipIndexUrl` | Conda 仓库和 pip simple 源 |
| `ChromeVersion`、`ChromeZipSha256` | Chrome for Testing 四段版本及完整 ZIP 的上游可信 SHA-256；更换版本时同时更新 |
| `GitUrl`、`ChromeZipUrl`、`FirefoxMsiUrl`、`EdgeMsiUrl`、`WiresharkUrl`、`NpcapUrl` | 相应完整安装包直链；设置后跳过该组件的默认版本查询 |
| `ChromedriverUrl`、`EdgedriverUrl`、`GeckodriverUrl` | 匹配当前浏览器的 Windows x64 驱动 ZIP 直链 |
| `GeckodriverVersion` | GeckoDriver 版本号，例如 `0.37.1`，必须与下载包或本地文件一致 |

地址必须是 HTTPS，不能填软件介绍页。Miniconda、Chrome ZIP 和 Wireshark 更换版本必须同时更新对应的可信哈希；不得使用会漂移的 `latest` 链接。系统组件及已验证兼容的驱动会复用；专用 Chrome 按指定版本重新解压。也可通过 `-InstallerDirectory` 提供同名完整包。

## 检查与排障

新版运行时会显示 `Windows Worker deployment revision: 2026-09-25.8`。如果旧版在 Miniconda 下载后报 `Installer signature invalid ... UnknownError`，覆盖脚本后直接重跑原命令即可；新版切换固定版本、核对官方哈希并自动处理旧缓存，不需要删除 `C:\TrafficWorker`。服务器上旧版错误的具体 Windows 原因无法仅凭 `UnknownError` 确定；新版日志会保留详细原因。

若旧版以 SYSTEM 启动后，Chrome/Firefox 已验收通过，而 Edge 报 `Microsoft Edge failed to start: crashed` / `DevToolsActivePort file doesn't exist`，先核对任务运行账号。Edge 官方仓库有[相同的 SYSTEM 启动问题](https://github.com/MicrosoftEdge/EdgeWebDriver/issues/100)。覆盖新版脚本后，在原管理员账号的提权 PowerShell 中执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Deploy\deploy_worker_windows.ps1 -RepairTaskOnly
```

在服务器的凭据提示中输入当前 Windows 账号的登录密码。此模式校验现有任务归属、检查是否有进行中的工作、备份配置与任务定义，然后将任务改为当前管理员账号的 Password 登录方式，并重新进行完整三浏览器验收。它不安装软件、不更新 Git/依赖、不更换 Worker ID 或 Token。自定义部署目录时仍需提供相同的 `-InstallRoot`；测试网站可用 `-SmokeUrl` 指定。执行前先暂停 Master 下发新任务。

若旧版卡在 `Downloading wireshark.exe ... 1.as.dl.wireshark.org`，尚未进入安装阶段，可按 Ctrl+C 停止当前部署，覆盖新版脚本后重跑；新版默认使用国内镜像。无需重新上传脚本时，也可在旧版的 `-DownloadConfig` JSON 中设置 `WiresharkUrl` 为 `https://mirrors.nju.edu.cn/wireshark/win64/Wireshark-4.6.9-x64.exe` 后重跑。

南大镜像的 `Wireshark-4.6.9-x64.exe` 已完整下载验证：97,931,232 字节，签名为 `Valid`，发布者为 `Wireshark Foundation`，SHA-256 与官方 `SIGNATURES-4.6.9.txt` 相同。镜像速度仍取决于服务器网络。

若 `2026-09-25.4` 在 Firefox 阶段报 `Invalid installer URL`，是旧版误拒绝了 `Firefox Setup ...msi` 中的空格。新版统一解析 HTTPS 地址并将空格编码为 `%20`，同样支持自定义下载地址，已编码的地址不会重复编码。已补充镜像 JSON → Firefox 地址解析 → 实际 HTTP 下载 → 文件哈希校验的本地联通测试；覆盖新版后重跑原命令即可。

如果旧脚本在 Git 下载阶段报 `Where-Object: 输入名称 name 无法解析为属性`，这是 Windows PowerShell 5.1 对 REST JSON 数组的管道处理差异。新版已修复 Git、Firefox 镜像列表及 Edge 产品列表的展开方式，并增加真实本地 HTTP JSON 响应回归测试。将新版脚本覆盖到 `C:\Deploy\deploy_worker_windows.ps1` 后重新执行原命令即可，无需删除 `C:\TrafficWorker`。

如果旧脚本在仓库克隆完成后报 `GetFullPath: 路径中具有非法字符`，原因是 Git 默认对 `使用说明.md` 等中文文件名加引号并转义。新版使用 `git ls-tree -z` 和显式 UTF-8 解码读取原始路径，已通过真实 Git 仓库的中文、空格路径测试。覆盖部署脚本后重跑即可，保留已经安装的 Git 和克隆好的项目。

在管理员 PowerShell 中执行：

```powershell
Get-ScheduledTask -TaskName TrafficCaptureWorker
Get-ScheduledTaskInfo -TaskName TrafficCaptureWorker
Get-Content C:\TrafficWorker\logs\worker.stderr.log -Tail 100
Invoke-RestMethod http://127.0.0.1:5100/api/v1/health
& 'C:\Program Files\Wireshark\tshark.exe' -D

# 维护时先暂停 Master 下发任务，再操作
Stop-ScheduledTask -TaskName TrafficCaptureWorker
Start-ScheduledTask -TaskName TrafficCaptureWorker
```

验收成功必须出现 `PASS: Chrome, Edge, Firefox captures; manifest, PCAP and TLS keylogs verified.`。只有 API 可用或计划任务处于 Running，不代表抓包已经通过。`-SkipSmoke` 会明确输出 `real captures NOT verified`。

默认使用简单的 HTTPS 文本页面验证浏览器、驱动、抓包和 TLS 密钥链路。它不代表任意复杂页面都已通过采集验收。Firefox 156 在关闭 HTTP 缓存后仍可能复用同一文档的图片；旧采集代码将这些响应全部判为失败。更新后的采集代码允许复用本次同一文档内已完整下载的图片，并将首次下载请求 ID 与复用记录写入 `network_status_firefox.json` 的 `network_summary.same_document_image_reuses` 及 `network_summary.cache_policy.same_document_image_reuses`。首次就命中缓存、缺少完整下载依据、304 或跨文档/跨 URL 缓存仍然失败，具体命中 URL 保留在 `cache_hits` 中。每个 URL 继续使用全新浏览器和 Profile；这项修复需要更新项目中的 `browser_firefox.py` 和 `browser_loading.py`，仅更换部署脚本不会自动改变正在运行的旧采集进程。

本机 Windows 的 Chrome 153、Edge 153、Firefox 156 已实际访问新版默认自检 URL，均通过网络静默及缓存检查并生成非空 TLS 密钥。图片复用修复通过 102 项相关单元测试及三浏览器真实缓存回归。Firefox 真实测试确认同页复用通过、两个采集 URL 对同一图片分别下载、历史页面缓存被拒绝；本轮没有运行 PCAP 解密测试，服务器端 PCAP 和最终任务结果仍需下面的验收命令确认。

如果 Worker 已正常运行，只想重跑简单页面验收，在服务器 PowerShell 中执行以下命令。它复用现有 API 和环境，不重装软件、不修改账号密码或计划任务触发器，也不改写运行配置。先等待已有任务结束并暂停 Master 下发任务；成功只表示这次简单页面验收通过，旧任务不会自动变为成功。

```powershell
@'
import json, runpy
from pathlib import Path
root = Path(r'C:\TrafficWorker\run')
runtime = json.loads((root / 'runtime.json').read_text(encoding='utf-8-sig'))
runtime.update(smoke_url='https://www.baidu.com/robots.txt', skip_smoke=False)
runpy.run_path(str(root / 'deploy_helper.py'))['verify'](runtime)
'@ | & 'C:\TrafficWorker\miniconda\envs\traffic-worker\python.exe' -
```

若出现抓包失败，查看部署输出中的测试任务路径及其 `worker.log`，确认 Npcap 已启动、TShark 能列出网卡、服务器能下载驱动并访问测试网站。不要只依据 PCAP 非空判断成功。需要交互安装 Npcap 时用远程桌面执行，普通 SSH 会话不适合安装向导。

本地已完成 PowerShell 5.1 语法与函数回归测试、配置保留/冲突检查、模拟 API 验收、真实 Windows 子进程清理、镜像 JSON 响应及 Git 中文路径测试。下载测试覆盖证书 `UnknownError`、哈希不符、坏缓存、备用源、损坏 ZIP、无签名 CfT、解压中断和环境中断后的恢复。

启动账号修复已通过不修改系统任务的回归测试：拒绝 SYSTEM/错误账号/空密码，注册定义使用 Password/Highest/开机触发，修复模式保留安装文件及 Worker 配置并调用真实抓包验收入口。**本地测试模拟了任务注册与启动，没有在本机保存真实账号凭据；目标服务器上的账号切换、三浏览器抓包和重启自启仍需实际验证。**

2026-09-25 实际下载核验：南大/北大 Miniconda 完整包均匹配官方 SHA-256；Git、Edge、Firefox、Wireshark、Npcap 的完整包签名均为 Valid；Chrome 官方与镜像 ZIP 的 SHA-256 完全一致。三个驱动均能执行 `--version`，专用 Chrome/ChromeDriver 用本项目的 headless 启动参数成功访问本地测试页面。**尚未在全新 Windows Server 上执行整套安装、重启自启及三浏览器真实抓包验收**；这些检查仍以目标服务器上的部署输出为准。
