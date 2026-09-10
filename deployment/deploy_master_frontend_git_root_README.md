# 从内嵌源码部署迁移到 Git 部署（root）

仓库：https://github.com/ThreeWater1037/Encrypted-Traffic-Capture-System.git

分支：`main`。脚本：[deploy_master_frontend_git_root.sh](deploy_master_frontend_git_root.sh)。不再携带内嵌源码。

## 执行

Windows PowerShell 上传：

```powershell
scp "D:\Project\Encrypted Traffic Capture System\deployment\deploy_master_frontend_git_root.sh" root@服务器公网IP:/root/
```

Ubuntu root 终端（选择没有采集任务的维护时间）：

```bash
bash /root/deploy_master_frontend_git_root.sh
```

首次会从 GitHub 克隆代码；如果原项目目录没有 `.git`，自动备份将被替换的文件，然后在原目录接入 Git。无需手动重命名整个目录或迁移数据库。

之后每次更新仍执行同一条命令。脚本获取 main 的确定提交，构建该提交的前端，然后通过 `git merge --ff-only` 更新服务器工作区。不会执行 reset --hard、git clean 或自动 stash。

## 路径、配置和数据

- 项目仍是 `/data/project/Encrypted-Traffic-Capture-System`。
- 环境仍是 `/data/miniconda/envs/Encrypted-Traffic-Capture-System-Master`。
- 页面仍是 `http://服务器IP:5173`，Master 在 `127.0.0.1:5200`。
- 保留 master.yaml、worker.yaml、master_data、worker_data、登录凭据及已有前端 releases。
- 原有 Master Token、数据路径等由配置生成逻辑保留；配置文件会备份并重新序列化。
- 将运行时文件加入服务器 `.git/info/exclude`，不修改仓库的 `.gitignore`。
- 仓库若开始跟踪上述配置、数据目录或前端 `.env`，脚本会拒绝部署。
- 全仓库纳入 Git，因此也会更新仓库内 Worker 代码。如果 traffic-worker 正在运行且 WorkingDirectory 正是项目目录，会暂停，并在成功完成后恢复；其他目录的 Worker 不处理。
- 备份位于 `/var/backups/traffic-console/时间戳/`，部署日志位于 `/var/log/traffic-console-deploy-时间戳.log`。
- 已有 Git 部署会保存 previous-commit.txt 和 target-commit.txt；首次迁移会保存 source-before-git.tar.gz。

旧配置、任务数据不需要重新创建。Node/npm、Conda、Nginx、systemd 和生产构建仍自动处理，所以该脚本也能用于新服务器。

## 更新保护

已有仓库必须满足：origin 与上述 URL 一致、当前分支为 main、没有已跟踪文件的未提交修改、当前提交可快进到待部署提交。

条件不满足时先自行检查：

```bash
cd /data/project/Encrypted-Traffic-Capture-System
git status
git remote -v
git log --oneline -5
```

需要保留的代码修改应在开发环境提交并推送，不要把配置或 Token 提交到仓库。脚本不会自动提交代码。

更新中断后服务可能保持停止；检查日志、Git 状态和服务状态，再继续部署。不会自动回滚软件包。若同目录 Worker 被暂停，失败后也需检查它是否需要手动启动。

## 完成后检查

```bash
cd /data/project/Encrypted-Traffic-Capture-System
git status --short
git log -1 --oneline
systemctl is-active traffic-master traffic-console-nginx
curl -fsS http://127.0.0.1:5200/api/v1/health
```

打开原页面，验证机器列表和任务数据仍在。新部署的浏览器账号用 `cat /etc/traffic-console/login.json` 查看。防火墙、登录方式和 Worker 接入方式与原部署保持一致。

**接入 Git 后，不再用旧的内嵌源码脚本更新代码**：旧脚本会覆盖文件，形成 Git 工作区修改。以后使用这个 Git 版脚本，它同时负责代码更新、依赖更新、前端构建和服务重启。

源码更新按完整 main 分支进行。前端构建使用暂存克隆中的源码，服务器前端 .env 不参与构建，固定采用同源 `/api/v1` 和服务器侧 Token 转发。
