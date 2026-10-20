# 独立离线 TLS 特征提取

`extract_tls.py` 使用 TShark 的 TCP/TLS 解析器，从 PCAP/PCAPNG 提取连接特征。只需要 **Python 3.8+ 和 TShark**，不需要编译或安装 nsdpf，也没有第三方 Python 包依赖。可以把此目录单独复制到其他机器。

当前在 Linux + Python 3.8 + TShark 3.2.3 上验证。实现使用跨平台标准库，无 `resource`、`fork`、systemd 或 shell 命令依赖；Windows/macOS 尚未实机验收。不同 TShark 版本可能有解析差异，脚本启动时会检查所需能力。

## 运行

先安装 Python 和所在平台的 Wireshark/TShark，确保 `tshark --version` 可运行。只读离线捕获通常无需管理员权限或抓包驱动配置。

Linux/macOS：

```bash
python3 extract_tls.py input.pcap -o features.jsonl
python3 extract_tls.py first.pcap second.pcapng -o batch.jsonl
```

Windows PowerShell（路径根据实际安装位置修改）：

```powershell
py -3 .\extract_tls.py .\input.pcapng -o .\features.jsonl --tshark "C:\Program Files\Wireshark\tshark.exe"
```

非标准端口如果没有自动识别，可明确使用 Decode As；不要把所有未知 TCP 数据强制认作 TLS：

```bash
python3 extract_tls.py input.pcap -o features.jsonl --decode-as 'tcp.port==8443,tls'
```

输出：

- `features.jsonl`：每个符合条件的 TCP 连接实例一行。
- `features.manifest.json`：输入、版本、解析选项、命令、统计、各输入对应的输出行范围及完成状态。

已有输出或报告会拒绝覆盖。再次运行使用新文件名。命令退出码为 0 且报告 `complete=true` 才表示完成；正常异常/中断会清理本次未完成结果。强制杀进程或断电无法保证清理，此时必须检查报告，不能把残留文件直接当成成功结果。

报告中的临时 `tls.keylog_file` 路径是一次性空密钥文件；复现命令时需替换为新的空文件。文件列表逐个独立分析，不自动拼接跨文件连接。同一文件内用 TShark 的 `tcp.stream` 区分五元组复用；不按五元组合并不同实例。结果顺序为输入顺序、连接首次出现顺序。

## 输出语义

JSONL 的字段集合和类型兼容 `json_flow_feature` 插件的 17 字段格式：

| 字段 | 含义 |
| --- | --- |
| `src_ip`, `src_port`, `dst_ip`, `dst_port` | 角色映射后的端点，支持 IPv4/IPv6 |
| `sni` | ClientHello 的 SNI，未知为空字符串 |
| `start_time`, `end_time`, `duration` | 有载荷 TCP 包时间的最小值、最大值及差，整数 Unix 毫秒 |
| `tcp_len` | 原始 TCP payload 长度，不含协议头，保留重传和重叠部分 |
| `tcp_pktdir`, `tcp_timestamps` | 对应 TCP 包的方向和捕获时间，整数 Unix 毫秒 |
| `tls_record_length` | TLS Record Header 的 body 长度，不含五字节 Header，允许零长度 |
| `tls_record_type` | 外层 Record 类型；TLS 1.3 加密 Record 为 23 |
| `tls_record_dir`, `tls_record_timestamps` | 对应 Record 的方向及 TShark 显示帧的捕获时间，整数 Unix 毫秒 |
| `tls_version` | ServerHello 确认的版本，TLS 1.3 优先取 supported_versions；未知或版本冲突为 `0x0000` |
| `timestamp` | 生成该行时的本机本地时间，`YYYY-MM-DD HH:MM:SS` |

方向为客户端到服务端 **1**，反向 **0**。TLS Hello 优先确定角色，其次 TCP SYN；无证据时以首个端点为 src，报告累计 `role_unknown_flows`。角色冲突时保留确定性的端点选择并统计 `role_conflict_flows`，不能将其解释成可信客户端。多个不同 SNI 以字典序第一个值输出，并累计 `sni_conflict_flows`。

TCP 数组按捕获帧顺序排列，纯 ACK/SYN/FIN 无 payload 时不进入数组，ACK+payload 不要求 PSH。TLS 数组按 **TShark 显示帧顺序和同帧 Record 顺序**排列：不是 TCP 包长，也不是同一方向字节偏移排序。

TLS 的显示帧时间在乱序、重组和两遍分析时可能不同于 nsdpf 的交付完成时间。因此这是格式兼容，而不是保证任意流量下与插件逐字段相同。非单调捕获时间也不用于重新排序数组。

## TLS 判据和解析边界

只有至少一条通过完整性检查的 TLS Record 的连接才输出，**不要求 SNI**。对每个 PDML Record 节点分别读取外层类型与长度，并在对应的原始 TCP payload 或 TShark 重组数据源中核对 Header 和完整 body；无法确认时排除并增加 `unverified_records`。接受外层类型 20–24、Record legacy_version 0x0300–0x0303；不支持 SSLv2。这个校验不是加密认证，也不能证明整条连接无丢包。

TShark 负责去重、乱序、IP/TCP 重组和 TLS/Handshake 解析。脚本不自行实现这些协议状态机，不依据重传标签预先删除包，也不凭长度相等删除 Record。没有被 TShark 识别或重组出来的 Record，脚本不能补出。

默认启用两遍分析、TCP 乱序重组，关闭名称查询、TCP checksum 校验及 TLS application-data 解密后的应用层重组；隔离个人配置、移除 `SSLKEYLOGFILE` 并提供空 TLS keylog。工具不主动提供解密密钥。PCAPNG 自带秘密或系统级插件/配置属于 TShark 环境的一部分，不应与普通无密钥基线混用；升级版本应重新对照样本。

仅面向 TCP 上的 TLS；不输出 QUIC/DTLS。遇到同帧多层 TCP/嵌套 TCP 协议树会明确失败，避免悄悄把隧道内外连接混合。IP 分片后的 TCP 包归属和多接口 PCAPNG 的连接判断沿用 TShark，不提供额外的接口域隔离。

没有内存预算、Record/TCP 包数量上限、分区、在线流池或异步 writer。连接特征在内存中聚合，解析成功后串行写文件；没有插件资源逐出造成的主动前缀截断，但原始缺包、捕获截断和解析器限制仍存在。批量输入也会保留结果到最终写出，本版不面向受限内存运行。

## 统计与验证

每个输入和全局都报告：

- `tcp_flows`：TShark 关联到的 TCP 连接数。
- `filtered_no_tls_record`：没有完整且通过校验 Record 的连接数，不等价于确定非 TLS。
- `output_flows`、`output_tls_records`：实际输出流数和 Record 数。
- `tcp_payload_packets`：全部关联到连接的有载荷 TCP 包数；`output_tcp_payload_packets` 只计保留流。
- `record_candidates`、`unverified_records`：TShark 显示的 Record 候选数及未通过校验数。
- `capture_truncated_packets`：抓包 caplen 小于 wirelen 的包数。

应满足 `tcp_flows = output_flows + filtered_no_tls_record`。`complete=true` 表示提取任务完成，不表示所有连接完整，也不是所有 TLS 流都已识别。

运行自包含的标准库测试：

```bash
python3 -m unittest discover -s tools/offline_tls -v
# 单独复制目录后：python3 -m unittest discover -s . -v
```

验证结果与已知差异见 [VALIDATION.md](VALIDATION.md)。TShark 用法参见[官方手册](https://www.wireshark.org/docs/man-pages/tshark.html)。
