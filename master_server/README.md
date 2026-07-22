# 主控 Flask

主控位于 Vue 前端和各子机器 Worker 之间，负责保存机器配置、分发任务、轮询
Worker，并把结果聚合到 `URL × 机器 × 浏览器` 粒度。PCAP、HTML、TLS 密钥等
实验大文件仍保存在各 Worker 的任务目录中。

## 本机端口

| 服务 | 地址 |
|---|---|
| Vue 前端 | `http://127.0.0.1:5173` |
| 主控 Flask | `http://127.0.0.1:5200` |
| 本机 Worker | `http://127.0.0.1:5100` |

## 安装与启动

```powershell
python -m pip install -r requirements-master.txt

$env:MASTER_HOST='127.0.0.1'
$env:MASTER_PORT='5200'
$env:MASTER_DATA_DIR='D:\TrafficCaptureMaster\data'
$env:LOCAL_WORKER_URL='http://127.0.0.1:5100'
$env:LOCAL_WORKER_TOKEN='与 Worker 相同的 Token'

python -m master_server
```

如果 Worker 和主控从同一个 PowerShell 窗口启动，也可以只设置一次
`WORKER_TOKEN`；主控在未设置 `LOCAL_WORKER_TOKEN` 时会继承它。

本机调试时可以不设置 `MASTER_TOKEN`。主控迁移到其他机器并对外监听时，应设置
随机 `MASTER_TOKEN`，同时在前端 `.env.local` 中配置：

```text
VITE_MASTER_API=http://主控IP:5200/api/v1
VITE_MASTER_TOKEN=与主控相同的Token
```

主控数据库和上传原文件分别位于：

```text
MASTER_DATA_DIR/master.db
MASTER_DATA_DIR/uploads/{job_id}/
```

## 启动顺序

```powershell
# 终端 1：先启动 Worker
python -m worker_agent

# 终端 2：启动主控
python -m master_server

# 终端 3：启动前端
cd frontend
npm run dev
```

进入前端后，如果本机 Worker 正常运行，主控会探测其 Chrome、Edge、Firefox、
TShark 和分析脚本能力。OpenAPI 文档见 [`openapi.yaml`](openapi.yaml)。

## 多机迁移

迁移主控不需要修改 Worker 代码。在“机器管理”中添加子机器的
`http://子机器IP:5100` 和对应 Token，并确保子机器防火墙只允许主控访问 5100。
浏览器前端始终只访问主控，不直接接触 Worker Token。
