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
- webdriver-manager 的驱动二进制放在 `WORKER_DATA_DIR/.wdm`；它只是运行依赖，
  不包含网页响应、Cookie 或实验结果

## 安装 Worker 依赖

Worker 服务与采集脚本可以使用不同 Python。Worker 服务环境安装：

```powershell
python -m pip install -r requirements-worker.txt
```

`PYTHON_EXECUTABLE` 指向已经安装 Selenium、webdriver-manager 等采集依赖的
解释器，例如：

```powershell
$env:PYTHON_EXECUTABLE='D:\Anaconda\envs\encrypted-traffic\python.exe'
```

## Windows 启动示例

```powershell
$env:WORKER_ID='win-01'
$env:WORKER_HOST='0.0.0.0'
$env:WORKER_PORT='5100'
$env:WORKER_TOKEN='请替换为随机长字符串'
$env:PROJECT_ROOT='D:\Project\Encrypted Traffic Capture System'
$env:PYTHON_EXECUTABLE='D:\Anaconda\envs\encrypted-traffic\python.exe'
$env:WORKER_DATA_DIR='D:\TrafficCaptureWorker\data'

python -m worker_agent
```

Worker 与实际采集程序共用浏览器自动发现逻辑，依次检查环境变量、PATH、Windows
App Paths 注册表以及 Program Files/LocalAppData 等安装目录。通常无需为不同子机器
修改代码。便携版或特殊安装位置可以显式覆盖：

```powershell
$env:CHROME_BINARY='E:\Browsers\Chrome\chrome.exe'
$env:EDGE_BINARY='E:\Browsers\Edge\msedge.exe'
$env:FIREFOX_BINARY='E:\Browsers\Firefox\firefox.exe'
```

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
  analysis = @{
    steps = @('extract', 'classify', 'infer')
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
| `analysis_steps` | 否 | `extract,classify,infer` |
| `with_coframe` | 否 | `false` |
| `sni_suffixes` | 否 | `wikipedia.org,wikimedia.org`；留空表示不过滤 |

浏览器前端示例：

```javascript
const form = new FormData()
form.append('task_id', 'upload-20260722-001')
form.append('file', fileInput.files[0])
form.append('browsers', 'chrome,firefox')
form.append('pcap', 'true')
form.append('analysis_steps', 'extract,classify,infer')
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
20 MiB、每个文件最多 10000 个 URL，可分别通过 `MAX_CONTENT_LENGTH` 和
`MAX_ITEMS` 调整。前端地址需要加入 `WORKER_ALLOWED_ORIGINS`。

Worker 会生成结构化的 `input.tsv`，再依次执行：

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

`manifest.json` 根据实际 HTML、PCAP、TSV、flows 和 inferred 产物判断
`SUCCEEDED`、`PARTIAL` 或 `FAILED`，不只依赖脚本退出码。

多 URL 任务完成后，`GET /api/v1/tasks/{task_id}` 和
`GET /api/v1/tasks/{task_id}/result` 都会返回 `items[]`。其中每个元素对应一个
URL，包含该 URL 的汇总 `status` 和 `browser_statuses[]`；顶层 `status` 仍表示
整个任务的汇总状态。`units[]` 保留每个“URL + 浏览器”组合的检查项和产物详情。
