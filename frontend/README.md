# FlowLab 主控前端

Vue 3 + Vite 页面已连接主控 Flask，不再使用 Mock 数据。页面支持：

- 粘贴 URL 或直接上传 UTF-8 TXT/TSV。
- 按 Worker 实际能力选择机器和 Chrome、Edge、Firefox。
- 默认只保存 PCAP 与 TLS keylog，并可选开启 HTML、报告、分析步骤、SNI 后缀和 coframe。
- 查看总任务及每个 `URL × 机器 × 浏览器` 的实时状态。
- 任务进度与已打开的 Worker 日志每 5 分钟自动刷新，适合发布任务后挂机。
- 顶部“立即刷新”和任务列表刷新按钮会立即查询任务列表、当前任务进度及已打开的日志；刷新期间禁用按钮，避免重复请求。
- 提交、切换任务和分页时立即查询，无需等待自动刷新。手动刷新读取 Master 已同步的进度，不额外触发 Worker 轮询。
- 采集期间根据逐 URL 原子检查点实时显示 `CAPTURED`，无需等待整批完成。
- 添加和探测子机器；Worker Token 不会显示在页面中。

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
