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

Copy-Item master.yaml.example master.yaml
notepad master.yaml

python -m master_server
```

主控默认读取项目根目录的 `master.yaml`，完整字段见根目录的
[`master.yaml.example`](../master.yaml.example)。真实配置可能包含主控 Token 和
Worker Token，已被 `.gitignore` 忽略。相对路径以 YAML 文件所在目录为基准。

配置优先级为：

```text
环境变量 > master.yaml > 程序默认值
```

如需从其他位置读取配置：

```powershell
$env:MASTER_CONFIG_FILE='D:\MasterConfig\master.yaml'
python -m master_server
```

原有 `MASTER_HOST`、`MASTER_PORT`、`MASTER_DATA_DIR`、`LOCAL_WORKER_TOKEN` 等
环境变量继续兼容，适合临时覆盖或由系统服务注入。

`bootstrap_worker.enabled` 默认为 `false`，此时机器完全由“机器管理”页面维护，删除后
重启主控也不会重新出现。只有设为 `true` 时，主控才会在每次启动时自动注册或更新
`bootstrap_worker.id` 对应的本机 Worker，其 Token 必须与本机 `worker.yaml` 一致。

本机调试时可以不设置 `MASTER_TOKEN`。主控迁移到其他机器并对外监听时，应设置
随机 `MASTER_TOKEN`，同时在前端 `.env.local` 中配置：

```text
VITE_MASTER_API=http://主控IP:5200/api/v1
VITE_MASTER_TOKEN=与主控相同的Token
```

主控数据库和上传原文件位于 `paths.data_dir` 指定的目录：

```text
paths.data_dir/master.db
paths.data_dir/uploads/{job_id}/
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
