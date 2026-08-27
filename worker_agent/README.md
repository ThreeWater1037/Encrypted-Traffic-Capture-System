# 子机器 Worker Flask

Worker Flask 将现有 `wiki_fetcher.py` 和 `batch_process.py` 包装为内部 HTTP
任务服务。它不直接向浏览器前端开放，不上传原始结果，也不在 HTTP 请求中
同步运行长任务。

## 固定约束

- 监听端口：`5100/TCP`
- API 前缀：`/api/v1`
- 单机采集并发：`1`
- 支持浏览器：Chrome、Microsoft Edge、Firefox
- Safari 暂不开放，因为当前实现无法提供与 Chrome、Edge、Firefox 等价的无缓存保证
- 所有任务使用全新目录，不传递 `--skip-existing`
- 所有 API 响应均包含 `Cache-Control: no-store`
- SQLite 只保存任务状态；实验文件保存在 `WORKER_DATA_DIR/tasks/{task_id}`
- 未完成任务在 Worker 重启后自动重新排队，并从逐 URL/浏览器原子检查点继续
- webdriver-manager 的驱动二进制放在 `WORKER_DATA_DIR/.wdm`；它只是运行依赖，
  不包含网页响应、Cookie 或实验结果

## 安装 Worker 依赖

Worker 服务与采集脚本可以使用不同 Python。Worker 服务环境安装：

```powershell
python -m pip install -r requirements-worker.txt
```

采集解释器还需要安装 Selenium 和 webdriver-manager：

```powershell
python -m pip install selenium webdriver-manager
```

## YAML 配置（推荐）

Worker 默认读取项目根目录的 `worker.yaml`：

```powershell
Copy-Item worker.yaml.example worker.yaml
notepad worker.yaml

python -m worker_agent
```

完整字段见根目录的 [`worker.yaml.example`](../worker.yaml.example)。真实
`worker.yaml` 包含 Token，已被 `.gitignore` 忽略。相对路径以 YAML 文件所在目录为
基准；`python_executable` 留空时使用启动 Worker 的解释器。

配置优先级为：

```text
环境变量 > worker.yaml > 程序默认值
```

自定义配置文件位置：

```powershell
$env:WORKER_CONFIG_FILE='D:\WorkerConfig\worker.yaml'
python -m worker_agent
```

原有 `WORKER_ID`、`WORKER_HOST`、`WORKER_TOKEN`、`PYTHON_EXECUTABLE` 等环境
变量继续兼容，适合临时覆盖或由系统服务注入，不再要求日常启动时逐项设置。

Worker 与实际采集程序共用浏览器自动发现逻辑，依次检查环境变量、PATH、Windows
App Paths 注册表以及 Program Files/LocalAppData 等安装目录。通常无需为不同子机器
修改代码。便携版或特殊安装位置可以在 YAML 的 `browsers` 分区填写绝对路径：

```yaml
browsers:
  chrome_binary: E:/Browsers/Chrome/chrome.exe
  edge_binary: E:/Browsers/Edge/msedge.exe
  firefox_binary: E:/Browsers/Firefox/firefox.exe
```

### WSL/Linux 浏览器代理

WSL 中的 Linux 浏览器不会可靠继承 Windows 系统代理，也不会自动使用终端里的
`HTTPS_PROXY`。访问维基百科等需要代理的站点时，在 `worker.yaml` 中显式设置：

```yaml
network:
  proxy_url: http://<Windows 主机在 WSL 中的地址>:7890
```

支持 `http`、`socks4` 和 `socks5`，必须填写端口。也可以用环境变量
`WORKER_PROXY_URL` 临时覆盖。代理程序需要允许来自 WSL 的连接；除非 WSL
镜像网络已经验证可用，否则不要默认把 Windows 代理写成 `127.0.0.1`。

配置只注入实验浏览器，不会改变主控访问 Worker API 的网络路径。Chrome、Edge 和
Firefox 仍然为每次实验创建全新无缓存 Profile。修改后需要重启 Worker。

安装 `waitress` 后会自动使用 Waitress；没有安装时仅为方便本机调试而回退到
Flask 开发服务器。多机使用时，应在防火墙中只允许主控机访问 5100 端口。

## API

OpenAPI 3.0 文档位于 [`openapi.yaml`](openapi.yaml)。在 Apifox 中选择“导入项目 →
OpenAPI/Swagger → 文件导入”，然后选择该文件。导入后在项目环境中将服务地址设为
`http://127.0.0.1:5100`，并将 Bearer Token 设为当前 `WORKER_TOKEN`。

| 方法 | 地址 | 说明 |
|---|---|---|
| `GET` | `/api/v1/health` | 健康、忙闲和队列状态 |
| `GET` | `/api/v1/capabilities` | OS、浏览器、TShark 和分析能力 |
| `POST` | `/api/v1/tasks` | 创建任务，立即返回 `202` |
| `POST` | `/api/v1/tasks/from-file` | 通过 multipart 直接上传 `.txt/.tsv` 创建任务 |
| `GET` | `/api/v1/tasks/{task_id}` | 查询任务状态 |
| `GET` | `/api/v1/tasks/{task_id}/log` | 按字节偏移增量读取日志 |
| `GET` | `/api/v1/tasks/{task_id}/result` | 查询本地结果清单 |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | 取消任务和整个子进程树 |

除健康检查外，请求必须携带：

```text
Authorization: Bearer <WORKER_TOKEN>
```

## 创建任务示例

```powershell
$headers = @{ Authorization = 'Bearer 请替换为随机长字符串' }
$body = @{
  task_id = 'exec-20260722-0001'
  items = @(
    @{
      id = '1'
      name = 'Python'
      url = 'https://en.wikipedia.org/wiki/Python'
    }
  )
  browsers = @('chrome')
  pcap = $true
  outputs = @{
    html = $false
    reports = $false
  }
  analysis = @{
    steps = @()
    with_coframe = $false
    sni_suffixes = @('wikipedia.org')
  }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:5100/api/v1/tasks' `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body $body
```

## 直接上传 txt/TSV

`POST /api/v1/tasks/from-file` 使用 `multipart/form-data`，文件必须是 UTF-8 编码，
每个有效行的格式为 `ID<TAB>名称<TAB>完整URL`。空行和以 `#` 开头的注释会忽略。

表单字段：

| 字段 | 必填 | 示例 |
|---|---|---|
| `file` | 是 | `urls.txt` 或 `urls.tsv` |
| `task_id` | 是 | `upload-20260722-001` |
| `browsers` | 否 | `chrome,edge,firefox`，默认 `chrome` |
| `pcap` | 否 | `true`，默认 `true` |
| `save_html` | 否 | `false`；额外保存页面 HTML |
| `save_reports` | 否 | `false`；额外保存抓取报告 |
| `analysis_steps` | 否 | 默认空；可选 `extract,classify,infer` |
| `with_coframe` | 否 | `false` |
| `sni_suffixes` | 否 | `wikipedia.org,wikimedia.org`；留空表示不过滤 |

浏览器前端示例：

```javascript
const form = new FormData()
form.append('task_id', 'upload-20260722-001')
form.append('file', fileInput.files[0])
form.append('browsers', 'chrome,firefox')
form.append('pcap', 'true')
form.append('save_html', 'false')
form.append('save_reports', 'false')
form.append('analysis_steps', '')
form.append('with_coframe', 'false')
form.append('sni_suffixes', '')

const response = await fetch('http://worker-ip:5100/api/v1/tasks/from-file', {
  method: 'POST',
  headers: { Authorization: 'Bearer <WORKER_TOKEN>' },
  body: form,
})
const task = await response.json()
```

不要手动设置 `Content-Type`，浏览器会自动生成 multipart boundary。默认请求上限为
256 MiB、每个任务最多 100000 个 URL，可分别通过 `MAX_CONTENT_LENGTH` 和
`MAX_ITEMS` 调整。前端地址需要加入 `WORKER_ALLOWED_ORIGINS`。

Worker 会生成结构化的 `input.tsv`，默认只执行采集命令；仅当
`analysis_steps` 非空时才执行第二行后处理：

```text
wiki_fetcher.py --input input.tsv ...
batch_process.py fetch_output --only extract classify infer --jobs 1
```

任务结果示例：

```text
worker_data/tasks/exec-20260722-0001/
├── request.json
├── input.tsv
├── worker.log
├── manifest.json
└── fetch_output/
```

`manifest.json` 默认根据实际 TLS keylog 和 PCAP 判断 `SUCCEEDED`、`PARTIAL` 或
`FAILED`，不只依赖脚本退出码。HTML、报告、TSV、flows 和 inferred 只在显式启用
时生成并加入校验。

多 URL 任务完成后，`GET /api/v1/tasks/{task_id}` 和
`GET /api/v1/tasks/{task_id}/result` 都会返回 `items[]`。其中每个元素对应一个
URL，包含该 URL 的汇总 `status` 和 `browser_statuses[]`；顶层 `status` 仍表示
整个任务的汇总状态。`units[]` 保留每个“URL + 浏览器”组合的检查项和产物详情。

## 长任务恢复语义

默认 `TASK_TIMEOUT_SECONDS=0`，即不设置整批任务总超时。每个采集单元成功落盘后会
原子生成 `capture_<browser>.complete.json`；只有标记中的 URL、浏览器和各产物精确
大小都匹配时，续跑才会跳过该单元。中断时正在写入的 URL 会清理半截文件并重抓，
此前已完成的 URL 不会重复访问。

`wiki_fetcher.py` 非零退出后，Worker 每 5 秒重启一次采集进程；Worker 自身重启后，
SQLite 中的未完成任务会恢复为 `QUEUED / RESUMING`。健康接口字段
`recovered_tasks_on_startup` 可用于监控本次启动恢复了多少任务。

默认容量目标是单任务 10 万 URL。百万级输入应拆成 1 万至 5 万 URL 的独立任务，
避免一个超大 HTTP 请求、结果 JSON 和 SQLite 事务成为单点故障。24 小时运行还需
由 systemd、Windows 服务或容器 restart policy 拉起 Worker；代码负责进程恢复后的
续跑，不负责在操作系统杀死进程后自行复活。
