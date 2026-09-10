# 空白 Ubuntu 服务器：单文件一键部署 Worker

需要后续用 Git 更新时，请改用 [Git 版部署与迁移脚本说明](deploy_worker_git_root_README.md)。迁移完成后，不要再通过本旧脚本覆盖内嵌源码。

脚本：[deploy_worker_root.sh](deploy_worker_root.sh)。新版内嵌当前 Worker 源码，空服务器无需预先上传项目、安装 Python、Conda、浏览器或 TShark。

## 使用方法

在本地 Windows PowerShell 上传这一个文件（替换服务器公网 IP）：

```powershell
scp "D:\Project\Encrypted Traffic Capture System\deployment\deploy_worker_root.sh" root@服务器公网IP:/root/deploy_worker_root.sh
```

在服务器 root 终端执行一条命令：

```bash
bash /root/deploy_worker_root.sh
```

脚本末尾包含压缩的源码。请直接上传完整文件，不要只复制前半部分，不要修改末尾 Base64 数据。不需要单独上传代码压缩包或执行 git clone。

## 自动使用的路径

| 内容 | 路径 |
|---|---|
| Worker 项目源码 | `/data/project/Encrypted-Traffic-Capture-System` |
| Miniconda | `/data/miniconda` |
| Python 环境 | `/data/miniconda/envs/Encrypted-Traffic-Capture-System` |
| 配置 | `/data/project/Encrypted-Traffic-Capture-System/worker.yaml` |
| 默认数据 | `/data/project/Encrypted-Traffic-Capture-System/worker_data` |
| systemd 服务 | `/etc/systemd/system/traffic-worker.service` |

`/data` 不存在时自动创建。若准备使用独立数据盘，应先将数据盘挂到 `/data`；脚本不格式化磁盘。没有独立挂载时使用系统盘空间。

## 自动完成

1. 检查 root、Ubuntu x86_64 和 systemd。
2. 校验内嵌压缩包 SHA-256，解包 Worker、采集和分析脚本、依赖清单。
3. 安装系统 Python、curl、证书、GnuPG、TShark、字体、UFW 等依赖。
4. 安装 Miniconda，创建 Python 3.12 环境，安装 Flask、Waitress、PyYAML、Selenium、webdriver-manager。
5. 安装 Chrome 和 Mozilla 官方 DEB 版 Firefox，核对 Mozilla 签名密钥指纹。
6. 固定 Firefox 路径 `/usr/lib/firefox/firefox`，避免此次遇到的 Snap 与独立 GeckoDriver 混用问题；不删除已有 Snap。
7. 实际检查 root 抓包、Chrome 和 Firefox 无头启动。
8. 创建 worker.yaml：5100 端口、随机 Token、任务总超时 0、10 万条输入上限、256 MiB 请求体上限。
9. 配置 root systemd 服务，自动 daemon-reload、开机自启并启动。
10. 检查 API、认证、Python 和浏览器路径，然后提交 Chrome + Firefox 单 URL 真实采集。
11. 仅在任务 SUCCEEDED、两浏览器 manifest 和 PCAP/TLS keylog 检查通过时报告采集验收成功。

内嵌的是生成脚本时本地 Worker 源码快照，不会自动拉取未来提交。包含 Worker 所需源码，不含 Master、前端、本地 Token、旧任务或 Git 数据。

## 服务器前提

- 标准 Ubuntu x86_64 云服务器，root 登录且 systemd 正常运行。
- 能访问 Ubuntu、Anaconda、conda-forge、Python 包源、Google、Mozilla、GitHub 驱动下载和测试网站。
- 不支持 ARM64、非 Ubuntu 或没有 systemd 的裸容器。
- 不需要桌面、Node.js 或手动激活 Conda。
- 默认机器 ID 为 `Ubuntu-worker-01`，多台部署用 `--worker-id` 设置不同名称。

## 常用参数

```bash
# 自定义服务器能访问的 HTTPS 测试网站
bash /root/deploy_worker_root.sh --smoke-url https://你能访问的测试站点/

# 设置唯一机器 ID
bash /root/deploy_worker_root.sh --worker-id Ubuntu-worker-02

# 添加只允许 Master 来源访问 5100 的 UFW 规则；换成真实 IPv4
bash /root/deploy_worker_root.sh --master-ip 203.0.113.10

# 网络慢时延长真实验收时限
bash /root/deploy_worker_root.sh --smoke-timeout 1200

# 只部署，跳过真实采集；不能据此认定采集已验收
bash /root/deploy_worker_root.sh --skip-smoke

bash /root/deploy_worker_root.sh --help
```

默认测试网站 `https://example.com`，时限 600 秒，包括驱动下载和队列等待。超时只取消脚本自己创建的测试任务。

目录可通过 `--project-dir`、`--conda-root`、`--conda-env` 分别调整，使用不含空格的绝对路径。

## 已有服务器与重复执行

默认复用已有项目代码、可用 Conda 环境，保留 Token、Worker ID、代理、数据目录、Edge 和 CORS 配置。重复运行会更新相关依赖和配置、重启服务并重新验收。

- `--update-code`：备份对应旧源码后安装内嵌源码，不删除任务和数据库。
- `--allow-interrupt`：明确允许停止正在采集的 Worker，否则检测到忙碌时退出。
- `--rotate-token`：生成新 Token，完成后需同步修改 Master Token。

部分解包的项目可用 `--update-code` 修复。Conda 目录存在但安装不完整时会停止并显示路径，不自动递归删除。

配置备份：`/var/backups/traffic-worker/时间戳/`。YAML 会重新序列化，注释不保留，原文件已备份。

日志：`/var/log/traffic-worker-deploy-时间戳.log`。日志不打印 Token，Token 保存在权限为 600 的 worker.yaml。

失败会返回非零并显示日志位置；Worker 可能保持停止或已启动但未通过验收。软件包不自动回滚，排错后可重新运行。部署过程中不要从 Master 提交新任务。

## 云安全组与 Master

脚本不持有云账号权限，云安全组仍需控制台配置。阿里云 Worker 入方向：允许、自定义 TCP、目标端口 5100、来源为 Master 实际公网出口 IPv4 `/32`。同 VPC 私网通信使用 Master 私网来源，不要填 Worker 自身公网 IP。

`--master-ip` 只添加对应 UFW 规则并输出安全组提示；不自动启用 UFW，不改 SSH 规则。启用 UFW 前先放行实际 SSH 端口。已有拒绝规则时还需检查防火墙规则优先级。

部署完成后在 Master“机器管理”添加：

- 地址：`http://Worker-IP:5100`，不添加 `/api/v1`。
- Token：从服务器 worker.yaml 读取。

跨公网长期使用可通过 VPN 或 HTTPS 保护 API 传输。脚本的 Worker 测试任务不自动显示在 Master，最后再从前端提交一个小任务验证跨机器连通性。

```bash
systemctl is-enabled traffic-worker
systemctl is-active traffic-worker
curl -fsS http://127.0.0.1:5100/api/v1/health
journalctl -u traffic-worker -n 100 --no-pager
```

服务启动后关闭 SSH 不影响运行。超时 0 不表示循环采集同一批网址；一批完成后等待新任务，持续运行仍取决于网站、磁盘和浏览器状态。

## 验证范围与来源

本地检查 Bash 语法、内嵌 Python 语法、源码包完整性、空目录解包和重复执行的源码处理。完整 Ubuntu 安装与真实采集须在服务器执行脚本时验证。

- [Mozilla DEB 安装](https://support.mozilla.org/en-US/kb/install-firefox-linux)
- [Snap 与 GeckoDriver 文件系统问题](https://firefox-source-docs.mozilla.org/testing/geckodriver/Usage.html)
- [Miniconda 非交互安装](https://www.anaconda.com/docs/getting-started/advanced-install/silent-mode)
