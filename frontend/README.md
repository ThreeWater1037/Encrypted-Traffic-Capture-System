# FlowLab 主控前端

Vue 3 + Vite 页面已连接主控 Flask，不再使用 Mock 数据。页面支持：

- 粘贴 URL 或直接上传 UTF-8 TXT/TSV。
- 按 Worker 实际能力选择机器和 Chrome、Edge、Firefox。
- 默认只保存 PCAP 与 TLS keylog，并可选开启 HTML、报告、分析步骤、SNI 后缀和 coframe。
- 查看总任务及每个 `URL × 机器 × 浏览器` 的实时状态。
- 任务进度每 5 分钟自动刷新，适合发布任务后挂机。
- Worker 日志仅在点击“读取日志／刷新日志”时读取，每台 Worker 显示最新 10 行，替换旧内容，不自动刷新或累积整份日志。
- 顶部“立即刷新”和任务列表刷新按钮仅刷新任务列表与当前任务进度，不读取日志；刷新期间禁用按钮，避免重复请求。
- 提交、切换任务和分页时立即查询，无需等待自动刷新。手动刷新读取 Master 已同步的进度，不额外触发 Worker 轮询。
- 采集期间根据逐 URL 原子检查点实时显示 `CAPTURED`，无需等待整批完成。
- 添加和探测子机器；Worker Token 不会显示在页面中。
- 任务历史支持服务端分页、名称/ID 搜索与状态筛选，顶部运行数统计全库任务。
- 支持按创建时间（最新/最早）、最近更新、名称（升序/降序）和进行中优先排序；排序在服务端分页前执行。
- 已结束任务可以从列表删除，页面内确认后仅隐藏任务记录，保留服务器采集文件、日志和检查点。进行中的任务需先取消或等待结束。
- 新任务的 Master ID 和 Worker 目录名使用“任务名-16 位哈希”，支持中文；非法路径字符会替换，长名称会截短，同名任务仍生成不同标识。
- 创建成功与刷新失败分别提示；任务操作防重复点击，切换任务时忽略旧请求的结果。
- 逐单元展示产物检查失败原因、保留的采集完成证据，以及可查看和复制的产物路径。
- 机器保存失败保留表单，工作台排除已删除、停用或不再支持的执行目标。
- 主控断连会标记数据可能过期；普通请求 30 秒、文件上传请求 120 秒超时。

## 回归验证

```powershell
cd frontend
npm test
npm run build
```

项目根目录的跨 Worker / Master 回归：

```powershell
py -3.13 -m unittest tests.test_job_management tests.test_frontend_regressions tests.test_master_server tests.test_worker_agent tests.test_master_openapi tests.test_openapi
```

本次状态修复需要同步更新 Worker、Master 和前端。更新代码不会自动改写此前已保存的错误历史结果；历史任务需另行从 Worker 产物重建结果，或由用户选择断点继续。

任务命名更新同样需要更新三端（建议先 Worker，再 Master 和前端）；旧任务沿用原有 ID、目录和已保存的 Worker 映射。删除为数据库软删除，重启后仍隐藏，已删除的 ID 不会被新任务复用。目前不提供恢复入口，也不清理服务器磁盘空间。

## 本地运行

```powershell
cd frontend
npm install --cache .npm-cache
npm run dev
```

默认页面为 `http://127.0.0.1:5173`，默认主控为
`http://127.0.0.1:5200/api/v1`。需要更换主控地址时创建 `.env.local`：

```text
VITE_MASTER_API=http://主控IP:5200/api/v1
VITE_MASTER_TOKEN=可选的主控Token
```

生产构建：

```powershell
npm run build
```
