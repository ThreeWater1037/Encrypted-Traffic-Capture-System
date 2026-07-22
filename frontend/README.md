# FlowLab 主控前端

Vue 3 + Vite 页面已连接主控 Flask，不再使用 Mock 数据。页面支持：

- 粘贴 URL 或直接上传 UTF-8 TXT/TSV。
- 按 Worker 实际能力选择机器和 Chrome、Edge、Firefox。
- 配置 PCAP、分析步骤、SNI 后缀和 coframe。
- 查看总任务及每个 `URL × 机器 × 浏览器` 的实时状态。
- 查看 Worker 本地实验路径与增量日志。
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
