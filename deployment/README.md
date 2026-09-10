# Ubuntu root 部署脚本

本目录集中存放 Worker、Master 和前端的部署脚本与说明，并纳入 Git 管理。

## 选择脚本

| 部署目标 | Git 版（用于新部署和后续更新） | 使用说明 |
|---|---|---|
| Worker | [deploy_worker_git_root.sh](deploy_worker_git_root.sh) | [Worker Git 部署说明](deploy_worker_git_root_README.md) |
| Master + 前端 | [deploy_master_frontend_git_root.sh](deploy_master_frontend_git_root.sh) | [Master 与前端 Git 部署说明](deploy_master_frontend_git_root_README.md) |

Git 版从项目仓库的 `main` 分支获取代码，支持空服务器安装、原有部署迁移和后续更新。默认使用 root、`/data/project/Encrypted-Traffic-Capture-System` 和 `/data/miniconda`；具体配置保留规则、停机范围和验收步骤见对应说明。

本机上传示例（Windows PowerShell，替换服务器 IP）：

```powershell
scp "D:\Project\Encrypted Traffic Capture System\deployment\deploy_worker_git_root.sh" root@服务器IP:/root/
```

服务器运行示例：

```bash
bash /root/deploy_worker_git_root.sh
```

后续也先上传最新脚本到 `/root/` 再运行，可避免运行中的脚本被本次 Git 更新替换。不要直接从将被更新的仓库目录运行部署脚本。

## 历史内嵌版本

- [Worker 内嵌脚本](deploy_worker_root.sh) · [说明](deploy_worker_root_README.md)
- [Master 与前端内嵌脚本](deploy_master_frontend_root.sh) · [说明](deploy_master_frontend_root_README.md)

内嵌版本携带生成时的源码快照，不会自动获取最新代码。已迁移到 Git 的服务器请使用 Git 版更新，避免旧快照覆盖仓库代码。

## Conda 与 Node 命令

部署服务使用绝对路径，不依赖当前终端是否激活 Conda。如需在 root 的 Bash 终端使用这些命令：

```bash
source /data/miniconda/etc/profile.d/conda.sh
/data/miniconda/bin/conda init bash
```

重新打开 Bash 终端后可以使用 `conda`。Master 环境中的 Node/npm 需激活环境后使用：

```bash
conda activate /data/miniconda/envs/Encrypted-Traffic-Capture-System-Master
node -v
npm -v
```

配置、Token、任务数据仍留在服务器运行目录，不应提交到 Git。本目录的 `.gitattributes` 将 Shell 脚本固定为 LF 换行。
