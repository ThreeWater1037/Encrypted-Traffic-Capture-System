# Ubuntu root：Git 版 Worker 部署与更新

脚本：`deploy_worker_git_root.sh`。固定仓库为 <https://gitee.com/Threewater1037/encrypted-traffic-capture-system.git>，分支为 `main`。

支持空服务器从零安装，也支持将原来的内嵌源码部署迁移为 Git 管理。只需上传脚本，代码由脚本克隆。服务器必须能访问 Gitee、Ubuntu 软件源、Miniconda、conda-forge、PyPI、Google、Microsoft、Mozilla 和测试网站（包括浏览器驱动下载地址）。

## 直接执行

本机 Windows PowerShell 上传文件，替换服务器 IP：

```powershell
scp "D:\Project\Encrypted Traffic Capture System\deployment\deploy_worker_git_root.sh" root@服务器IP:/root/
```

服务器 root 终端执行：

```bash
bash /root/deploy_worker_git_root.sh
```

后续先将开发电脑上的代码提交并推送到 Gitee 的 `main`，然后在服务器上执行同一命令更新。不需要自己删除项目、覆盖源码、执行 `git pull` 或激活 Conda。

如果服务器之前使用 GitHub 仓库，首次运行新版脚本前，先将服务器项目的 `origin` 切换为 Gitee（自定义部署目录请替换路径）：

```bash
git -C /data/project/Encrypted-Traffic-Capture-System remote set-url origin https://gitee.com/Threewater1037/encrypted-traffic-capture-system.git
```

Gitee 的 `main` 必须包含服务器当前提交并支持快进更新；脚本仍会拒绝历史分叉或本地已跟踪文件有修改的情况。

**迁移后不要再用旧的 `deploy_worker_root.sh --update-code` 更新代码，它会覆盖为旧的内嵌快照。** 新脚本无 `--update-code` 参数，每次运行都会检查并更新 Git 代码。

## 默认路径与环境

| 内容 | 路径或设置 |
|---|---|
| 项目目录 | `/data/project/Encrypted-Traffic-Capture-System` |
| Miniconda | `/data/miniconda` |
| Worker Conda 环境 | `/data/miniconda/envs/Encrypted-Traffic-Capture-System` |
| root Bash 默认环境 | 新开的交互式终端自动激活上述项目环境 |
| 配置文件 | `/data/project/Encrypted-Traffic-Capture-System/worker.yaml` |
| 默认数据目录 | `/data/project/Encrypted-Traffic-Capture-System/worker_data` |
| root 服务 | `traffic-worker.service` |
| 监听 | `0.0.0.0:5100` |
| 日志 | `/var/log/traffic-worker-deploy-时间戳.log` |
| 备份 | `/var/backups/traffic-worker/时间戳/` |

需要 Ubuntu x86_64、root 和运行中的 systemd。新环境安装 Python 3.12；已有环境需 Python 3.10 以上。脚本自动安装 Git、TShark、字体、Miniconda、Python 依赖、Chrome、Edge 和 Firefox，不需要桌面环境或 Node.js。

Edge 使用 [Microsoft 官方 APT 仓库](https://packages.microsoft.com/repos/edge/) 的 `microsoft-edge-stable`，安装前校验 [Microsoft 签名密钥指纹](https://learn.microsoft.com/en-us/linux/packages#how-to-use-the-gpg-repository-signing-key)，配置为 `/usr/bin/microsoft-edge-stable`；该路径已存在可执行文件时直接复用。脚本备份并使用 `microsoft-edge.list`，若其他源文件已配置 Edge 仓库，会提示先合并以避免重复源。

Firefox 沿用已经验证可启动的 Mozilla DEB 安装方式，配置为 `/usr/lib/firefox/firefox`。软件源和密钥指纹依据 [Mozilla 官方安装说明](https://support.mozilla.org/en-US/kb/install-firefox-linux)，不会删除已有 Snap。

## 首次迁移和后续更新

1. 克隆并检查远端 `main`，记录目标提交。
2. 检查配置、数据路径以及源文件目录，拒绝覆盖符号链接或把数据当源码处理。
3. 检查当前 Worker 是否有运行中或排队的任务；默认有任务时退出。
4. 停止 Worker，再更新代码和依赖。
5. 非 Git 目录：先备份将被替换的源文件，然后导入仓库源码与 `.git`；已有 Git 目录：要求 origin 匹配、分支为 main、已跟踪文件无修改，并且能快进更新。
6. 更新配置，安装并启动 root systemd 服务，启用开机自启和退出后自动重启。
7. 检查健康接口、认证、解释器和浏览器路径，然后进行 Chrome、Edge、Firefox 的真实 HTTPS 抓包测试，验证三个浏览器各自的 manifest 记录、PCAP 和 TLS keylog。
8. 备份 `/root/.bashrc`，写入项目 Conda 环境自动激活配置；重复部署会更新同一配置块，不会重复追加，并跟随 `--conda-root`、`--conda-env` 的设置。

部署成功后，新开的 root 交互式 Bash 终端默认进入项目 Conda 环境。脚本通过子进程运行，无法改变当前终端的环境；当前终端执行一次 `source /root/.bashrc` 即可生效。非交互式脚本不自动激活。

克隆的是完整仓库，但本脚本只安装、启动 Worker。**如果同一项目目录已配置为 Master 服务的工作目录，脚本会拒绝更新，避免同时更改 Master 后端却没有重建前端。** 同机运行时请让 Worker 使用单独的项目目录，例如 `--project-dir /data/project/Encrypted-Traffic-Capture-System-Worker`；迁移已有 Worker 到不同目录时，先复制其配置并保持原数据目录的绝对路径，再执行部署。

运行期间请暂停从 Master 下发新任务；任务状态检查与停止服务之间并非原子操作。脚本会短暂停机，也可能因浏览器、驱动下载或验收耗时较长，建议在维护窗口执行。

## 保留哪些内容

- 保留现有 Token、Worker ID、代理、数据目录、CORS 和已有任务限制；未配置的字段补默认值。
- 固定端口 5100、监听地址、当前项目/Python 路径及 Chrome/Edge/Firefox 路径；已有自定义 Edge 路径会改为上述稳定版路径。
- 不清空任务、PCAP、TLS keylog 或其他未跟踪文件。旧的多余源码也不会自动清理。
- 原 `worker.yaml` 和服务文件会备份；非 Git 迁移备份为 `source-before-git.tar.gz`；已有仓库保存 `previous-commit.txt`，目标保存为 `target-commit.txt`。
- 不使用 `git reset --hard` 或 `git clean`。本地已跟踪文件有修改、分支不符、历史领先或分叉时，停止并给出原因。
- 数据和密钥不应提交到 Git；脚本检查当前及目标仓库中的常见运行时路径，并使用 `.git/info/exclude` 添加本机忽略规则。

源码备份不包含完整任务数据和整个 Conda 环境，不能作为整机备份。发生错误时不会自动回滚：Worker 可能停止，或已启动但验收未通过，需要查看日志、修复后重新执行。

## 常用参数

```bash
# 多台 Worker 使用不同 ID；已有 ID 默认保留
bash /root/deploy_worker_git_root.sh --worker-id Ubuntu-worker-02

# 测试网站必须是服务器可访问的 HTTPS 地址
bash /root/deploy_worker_git_root.sh --smoke-url https://example.com --smoke-timeout 900

# 只部署并检查 API，跳过真实抓包验收
bash /root/deploy_worker_git_root.sh --skip-smoke

# 明确允许中断现有任务；通常应等任务结束再更新
bash /root/deploy_worker_git_root.sh --allow-interrupt

# 更换 Token，之后必须同步修改 Master 中的 Worker Token
bash /root/deploy_worker_git_root.sh --rotate-token

# 可选：给 UFW 添加只允许 Master IPv4 访问 5100 的规则
bash /root/deploy_worker_git_root.sh --master-ip 你的Master出口IPv4

bash /root/deploy_worker_git_root.sh --help
```

脚本不会自动启用 UFW、修改 SSH 或操作云安全组。云安全组需另行放行 TCP 5100，来源填写 Master 实际出口 IP `/32`；内网访问填写对应私网来源。

## 验收与排障

引导检查使用 `/usr/bin/python3 -I`，确保读取 Ubuntu `python3-yaml` 包，不受当前终端激活的 Conda 环境影响；Worker 仍使用上表指定的 Conda Python。旧版脚本在 `(base)` 下可能报 `No module named 'yaml'`，可先执行下面的命令确认系统依赖，再用系统 PATH 运行旧脚本：

```bash
/usr/bin/python3 -I -c 'import yaml; print(yaml.__file__)'
env -u PYTHONHOME -u PYTHONPATH PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /bin/bash /root/deploy_worker_git_root.sh
```

正常完成时，输出包含 Chrome、Edge 和 Firefox 抓包 `SUCCEEDED`，并验证 manifest、三种浏览器各自的 PCAP 与 TLS keylog。`active` 或健康接口 `ok` 只表示服务可用，不能单独证明抓包正常。`--skip-smoke` 会明确标记未做真实抓包验收。

```bash
systemctl status traffic-worker --no-pager
journalctl -u traffic-worker -n 100 --no-pager
curl -fsS http://127.0.0.1:5100/api/v1/health
git -C /data/project/Encrypted-Traffic-Capture-System status --short
git -C /data/project/Encrypted-Traffic-Capture-System log -1 --oneline
```

Master 添加 Worker 的地址为 `http://Worker-IP:5100`，不要附加 `/api/v1`；Token 从服务器 `worker.yaml` 本地读取。脚本直接提交的测试任务不会出现在 Master 任务列表中，最后仍需在前端向该 Worker 下发一次任务，确认完整链路。

如果提示 `mozilla.list` 与 `mozilla.sources` 重复，先检查 `/etc/apt/sources.list.d/`，整理已有 Mozilla 软件源后重跑。如果 Gitee 不通，先解决服务器到 Gitee 的网络访问。不要通过删除项目目录或强制重置来跳过错误。

脚本已做本地 Bash 语法和隔离仓库逻辑验证；系统依赖、浏览器和 systemd 的最终结果以目标 Ubuntu 上的执行及抓包验收为准。
