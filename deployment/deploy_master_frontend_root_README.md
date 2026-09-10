# 空服务器一键部署前端和 Master（root）

脚本：[deploy_master_frontend_root.sh](deploy_master_frontend_root.sh)。内嵌前端及 Master 当前源码，从空 Ubuntu x86_64 服务器安装整个运行环境，无需先上传项目或 git clone。

## 两步使用

在本地 Windows PowerShell 上传一个脚本：

```powershell
scp "D:\Project\Encrypted Traffic Capture System\deployment\deploy_master_frontend_root.sh" root@服务器公网IP:/root/
```

在服务器 root 终端执行：

```bash
bash /root/deploy_master_frontend_root.sh
```

请上传完整文件，脚本末尾是内嵌源码数据，不要删改。

## 自动安装和配置

- Python 3.12、Miniconda、Node.js 22、npm。
- Flask、Waitress、PyYAML 等 Master 依赖。
- 前端 `npm ci` 安装锁定依赖，执行 `npm run build` 生产构建。
- Nginx 提供静态页面，通过 `/api/` 转发请求给 Master。
- 创建 Master 配置、随机 Master Token 和浏览器访问密码。
- 配置两个 root systemd 服务，开机自启、退出后自动重启。
- 验证 Master 健康、认证、页面、静态资源、Nginx 代理 API，以及未登录请求被拒绝。

浏览器打开页面后会弹出 HTTP Basic 登录框。Master Token 只保存在服务器配置中，由 Nginx 转发，不写进前端 JavaScript。浏览器登录密码与 Worker Token、Master Token 是不同的凭据。

## 路径和端口

| 内容 | 默认值 |
|---|---|
| 项目 | `/data/project/Encrypted-Traffic-Capture-System` |
| Miniconda | `/data/miniconda` |
| Master 专用环境 | `/data/miniconda/envs/Encrypted-Traffic-Capture-System-Master` |
| Master 配置 | `/data/project/Encrypted-Traffic-Capture-System/master.yaml` |
| 新部署 Master 数据 | `/data/project/Encrypted-Traffic-Capture-System/master_data` |
| 前端构建文件 | 项目目录下 `frontend/releases/时间戳/` |
| Nginx 专用配置 | `/etc/traffic-console/nginx.conf` |
| 浏览器登录凭据 | `/etc/traffic-console/login.json`，root 可读 |
| 页面地址 | `http://服务器公网IP:5173` |
| Master API | `127.0.0.1:5200`，仅本机监听 |
| Master 服务 | `traffic-master` |
| 页面及代理服务 | `traffic-console-nginx` |

沿用此前 `/data` 项目布局，Master 使用独立 Conda 环境，避免更新 Python/Node 时影响 Worker 环境。脚本不安装浏览器和 TShark，不修改 worker.yaml，不启停 Worker 服务。

如果 `/data` 没有挂载独立数据盘，目录使用系统盘。脚本只创建目录，不分区或格式化磁盘。需要数据盘时请先完成挂载。

## 部署后登录

部署成功后，在 root 终端查看登录信息：

```bash
cat /etc/traffic-console/login.json
```

浏览器访问：

```text
http://服务器公网IP:5173
```

输入生成的 `admin` 用户名及密码。密码不会打印到部署日志。默认 HTTP 不加密密码和业务数据，长期公网访问应配置 HTTPS 或使用 VPN，并把安全组来源限制为管理网络。

这是一套长期服务，不依赖 SSH 会话，不使用 `npm run dev` 保持页面。Node 只用于构建；运行阶段由 Nginx 和 Master 提供服务。

## 云安全组

在前端/Master 这台机器上添加入方向：允许 TCP 5173，来源填写你的管理电脑公网出口 IP `/32`。保持 SSH 端口规则。不要开放 5200；页面通过同源 Nginx 代理调用 Master。

脚本不修改云安全组、UFW 或 SSH。若服务器的 UFW 已启用，还需放行实际访问来源：

```bash
ufw allow from 管理电脑公网IP to any port 5173 proto tcp
```

阿里云安全组仍需在控制台设置。若原先系统已经安装 Nginx，本脚本保留它，使用独立配置、PID 和服务运行；若本次新安装 Nginx，则关闭软件包自动启动的默认 80 端口站点。

## 接入已经部署的 Worker

登录页面，在“机器管理”添加：

- Worker 地址：`http://Worker服务器IP:5100`，不要追加 `/api/v1`。
- Worker Token：Worker 的 worker.yaml 中的 token。

Worker 云安全组的 5100 来源应允许新 Master 服务器的实际出口 IP；若同 VPC 内网通信，使用 Master 私网来源和 Worker 私网地址。之前只允许本地电脑访问 Worker 的规则不一定允许新 Master。

同机同时部署 Worker 时，可填 `http://127.0.0.1:5100`。默认不自动添加任何 Worker。添加后刷新能力，并从前端提交单 URL 测试验证完整链路。

## 重复运行与运维

重复执行会先备份并替换脚本所包含的前端和 Master 源码，更新环境、构建页面并重启服务；不删除 Master 数据库、任务、机器列表、上传文件或 Worker 数据。运行前选择维护窗口，避免中途改变正在处理的任务。

- 已有 Master Token、数据目录、限制值和显式 bootstrap_worker 配置默认保留。
- 新机器生成随机 Master Token，关闭自动注册本机 Worker。
- master.yaml 会重新序列化，注释不保留，原文件已备份。
- 已有浏览器登录账号和密码保留。
- 脚本内嵌的是生成时源码快照，不自动跟踪后续 Git 提交。
- 备份：`/var/backups/traffic-console/时间戳/`。
- 部署日志：`/var/log/traffic-console-deploy-时间戳.log`。
- 失败会返回非零并指出日志，服务可能停止或部分完成；修复后可重跑，不会自动回滚软件包。

```bash
systemctl status traffic-master traffic-console-nginx --no-pager
systemctl is-enabled traffic-master traffic-console-nginx
systemctl is-active traffic-master traffic-console-nginx
journalctl -u traffic-master -u traffic-console-nginx -n 100 --no-pager
curl -fsS http://127.0.0.1:5200/api/v1/health
```

修改 Master Token 后必须同步更新 Nginx 配置中的转发 Token，可重新执行部署脚本；只改 master.yaml 而不改 Nginx 会导致页面 API 返回 401。

需要更改页面端口时：

```bash
bash /root/deploy_master_frontend_root.sh --web-port 8080
```

同时修改安全组和 UFW。其他参数：`--project-dir`、`--conda-root`、`--conda-env`，使用无空格绝对路径。

## 验证范围

脚本执行时验证服务和前端代理，不自动创建采集任务或猜测 Worker 地址。实际浏览器交互与 Master → Worker 采集需在添加 Worker 后测试。

本地验证源码包完整性、Bash/Python 语法、前端生产构建和 Master API；空白 Ubuntu 上的 apt、Conda、systemd 和 Nginx 联合验收需要在目标服务器运行。

参考：[Vite 6](https://v6.vite.dev/guide/)、[Nginx Basic Authentication](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)。
