# 已完成任务的独立提取

前端入口为“任务提取”（`#extractions`）。选择服务器及成功或部分成功的采集任务，勾选“流序列”“TLS 特征”中的一项或两项（默认两项），设置“保留原始 PCAP”，点击“开始提取”。不勾选保留时，所选内容校验成功后清理；勾选可选择全部保留或仅保留前 N 个。若需要之后补提另一项，应保留原始 PCAP。原任务的 Chrome、Edge、Firefox 单元全部按相同规则处理，无需重新启动浏览器。

前 N 个按原任务 URL 列表顺序、同一 URL 内 Chrome → Edge → Firefox 顺序计数，仅包含原任务启用的浏览器；每个浏览器计一个单元。缺失或失败单元仍占原顺序位置，失败项额外保留，N 超过总数时全部保留。保留策略在首次提交时持久化，取消、重启和重试沿用该策略，改变策略的重复提交返回 409。已被此前流程清理的 PCAP 无法恢复，要求保留但原包已不存在的单元会报告失败。

## 调度与恢复

- 独立提取和抓包由同一个 TaskManager 消费线程执行，绝不同时运行。提取在抓包队列为空时开始；新抓包任务到来时，当前 PCAP 处理完成后让出执行权。当前 PCAP 的全部阶段上限为 6000 秒，每个阶段上限为 900 秒。
- 每台 Worker 的提取任务按首次入队时间执行；不同服务器各自拥有队列。
- 原任务只接受 `SUCCEEDED`、`PARTIAL`，且必须开启 PCAP。API 再次检查 Worker 实时状态，不能依靠前端缓存绕过。原任务有活动提取时拒绝断点续抓，避免改写输入。
- 独立提取记录按原任务 ID 和所选内容生成确定性哈希，双选沿用旧 ID，保存在既有 Worker SQLite 中。同一原任务不能同时运行不同选项的提取。重复提交不会重复排队。失败、部分成功、取消后重新提交会重新检查全部单元，已有校验凭据覆盖的成功项只复核，不重跑分析。
- 队列、当前游标、失败信息、成功保留和已删除数量持久化。重启恢复时按校验凭据处理“已经删除 PCAP，但尚未写入进度”的窗口。
- 原来的 `analysis.pipeline` 抓包期间后台提取、其开关和默认值均保留。复用已有校验结果时，现存 PCAP 是否删除以本次调用的删除开关为准，防止历史待删除凭据覆盖当前保留选择。

## 输出与删除

每个 PCAP 只执行所选内容。“流序列”包括流特征、解密分类、推断、流序列生成和校验，需要 TLS 密钥；“TLS 特征”直接读取 PCAP，不要求密钥，也不运行流序列步骤。双选时先流序列、后 TLS 特征。输出仍位于原 URL 目录下：

```
flow_extraction/<browser>/attempt-*/sequences/decrypted/{forward,reverse,bidirectional}.tsv
flow_extraction/<browser>/attempt-*/sequences/inferred/{forward,reverse,bidirectional}.tsv
flow_extraction/<browser>/attempt-*/tls/capture_<browser>.jsonl
extraction_<browser>.json
extraction_<browser>.progress.json
```

沿用现有的源文件签名、流序列校验、TLS 完整性和输出文件 SHA-256 校验，所选内容全部成功且该单元不在保留范围内才删除原始 PCAP。TLS 密钥、检查点和结果保留。部分成功任务中缺少完整检查点、丢包或流序列所需密钥的单元记为失败，并保留原包。

版本 2 校验凭据累计记录已完成的 `steps`、各类 `result_dirs` 和输出签名，兼容旧版双选凭据。后续补提只执行缺少的内容；源文件变化时不复用旧结果。补提失败不会覆盖之前有效的校验凭据，错误与当前阶段写入 `.progress.json`。结果索引分别列出流序列和 TLS 产物，不将未选内容标记为成功。

原采集任务的请求和历史完成状态不改写。提取结束后更新 Worker manifest；打开提取详情时，Master 同步原任务的最新文件索引，让“任务与结果”“数据与结果”正确显示 PCAP 已清理和新产物位置。取消时已完成单元不会回滚；再次提交可根据凭据恢复。

## API

- Worker：`GET/POST /api/v1/extractions`、`GET /api/v1/extractions/<id>`、`POST /api/v1/extractions/<id>/cancel`。
- Master：`GET /api/v1/extractions/candidates`；以 `/api/v1/machines/<machine_id>/extractions` 转发列表、提交、详情和取消。
- 列表为数据库分页；失败明细按需加载，最多最近 100 条。完整日志保存在 `tasks/<extraction_id>/worker.log`。
- Worker 尚未升级时，页面明确说明接口不可用，不会回退到旧后台提取模式。
- 提交可携带 `steps`（非空、不重复的数组，值为 `flow`、`tls`；省略或 null 默认双选）。列表及详情的每个 job 返回 `steps`。Worker 列表返回 `selectable_extraction_steps: [flow, tls]`；Master 单选提交前检查该能力，旧 Worker 不支持时返回 409。
- 提交可携带 `keep_pcap`（布尔，默认 false）及 `keep_pcap_count`（正整数或 null，null 表示全部保留）。未开启保留时禁止指定数量。Master 在保留请求入队前检查 Worker 列表的 `pcap_retention_supported=true`，防止旧 Worker 忽略参数后删包。

## 无干扰发布要求

这次功能先在本地实现与验证。上线需要一起发布 Master、前端及 Worker 新接口。
不能为了加载新接口重启正在抓包的 Worker，也不要直接覆盖活跃 Worker 的 Python 源码，因为后续子进程会重新导入。应先等待该 Worker 运行中和排队的抓包任务全部结束，再备份、部署并验收。现有任务不自动加入提取队列。
