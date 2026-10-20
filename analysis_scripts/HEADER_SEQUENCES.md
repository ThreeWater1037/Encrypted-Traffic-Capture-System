# Header 长度序列提取

新增入口 `extract_header_seqs.py`，保留 `extract_forward_seqs.py` 的旧入口和输出。
新入口从已有逐包 TSV 提取两种模式、三个方向，共六类汇总；不会改动 PCAP 或 TLS 密钥。

## 2026-10-12 失败项恢复

包级提取以 TCP stream 和端点共同区分连接，支持同一四元组被多个连接复用。
无文件名冲突时保留端口命名；冲突连接的两个方向使用 `_tcp_<stream-id>` 后缀。
逐包文件的注释新增 `tcp_stream`，汇总 TSV 列保持不变。
SNI、TLS 版本按当前连接重新取证，旧端点索引中可能跨连接合并的应用协议提示清为
`other`；解密协议仍以 `observed_app_protocol` 和实际 HTTP 解析为准。
源、目标端口均为 443 时依据 SYN/SYN+ACK 确定方向；无证据时显式失败。

ICMP/ICMPv6 错误报文引用的 TCP 头不再计入实际 TCP 流。
包级时间使用原 PCAP 的 `frame.time_relative`，不因过滤报文而改变时间原点。
三个浏览器共用同一提取实现；本次修改不涉及浏览器启动或采集生命周期。

只重试旧批处理 `results.jsonl` 中最新状态为失败的条目：

```bash
python -m analysis_scripts.retry_failed \
  /data/forbes_extraction_20261010_run \
  /data/forbes_extraction_retry_20261012_run --workers 2
```

输入需要旧报告的 `pcap/keylog/browser/before` 等元数据。仅读取原 PCAP 和密钥，
每次尝试创建 `flow_extraction_retry_20261012/<browser>/attempt_N/`，保留历史失败输出。
默认保留 8 GiB 磁盘空间及 100000 个 inode；同一新报告目录使用进程锁防止重复运行。
重试前后验证原输入的 SHA-256、大小、纳秒修改时间，且与旧批次签名核对。
成功和已明确受数据限制的条目不会重复处理，失败条目再次执行时创建新 attempt。

结果状态：

| 状态 | 含义 |
|---|---|
| `success` | 原 PCAP 完整，两种分析模式及逐包、方向汇总校验通过；不表示每条 TLS 流均可解密 |
| `partial` | 缺失/空密钥只产出不解密模式，或损坏 PCAP 仅恢复可读前缀；限制写入 validation.json |
| `no_eligible_packets` | 可读部分没有实际 TCP/443 包，无法生成目标流序列 |
| `failed` | 仍存在需要排查的代码、环境或输入错误 |

仅对 TShark 明确报告的截断/损坏执行恢复：`editcap` 将完整可读记录写入新的
`recovered_input.pcapng`，再确认其可独立读取且帧号、长度、捕获长度、时间戳与原输入可读前缀一致。
原 PCAP 不修改、不删除。不能重建已丢失的尾部、TLS 密钥或不存在的 TCP/443 包。
独立校验器逐包验证 stream、端点、时间、包长、载荷长、TCP flags 与唯一覆盖，
并验证双向汇总的分类计数和事件数量守恒。

本地回归包括真实 TShark 的连接复用、ICMP 引用排除、三浏览器文件标签、损坏文件恢复；
服务器验收使用实际存量 Chrome/Edge/Firefox PCAP，不重新发起网页采集。

已增加 HTTP/1.1 解密头部提取。默认 `--protocol both` 将 HTTP/1.1 和 HTTP/2 合并到
同一组三个方向文件，通过新增 `protocol` 列标记 `HTTP/1.1` 或 `HTTP/2`。
加上原有不解密结果，共六个汇总 TSV，不再创建协议子目录。

## HTTP/1.1 新增用法

先重新运行 `classify_packets.py`（或者 batch_process 的 classify 步骤，勿使用 skip-existing），
让解密逐包 TSV 包含 `http1_header_lens`、`http1_message_types` 和协议观测元数据。
然后运行：

```bash
python analysis_scripts/extract_header_seqs.py /path/to/task --browser chrome --protocol both --allow-unpaired
```

`--browser edge`、`--browser firefox` 使用同一实现。若未确认缺向连接是否确实只有一个方向，
可省略 `--allow-unpaired`，保留严格检查。

新输出为：

```text
header_sequences/chrome/
  inferred/{forward,reverse,bidirectional}.tsv
  decrypted/{forward,reverse,bidirectional}.tsv
  summary.json
```

TCP 标志细分后两种汇总均增加 `syn/syn_ack/fin/fin_ack/rst/rst_ack/tcp_other` 七列，
替代粗略的 `tcp_control`。不解密为 19 列，解密额外在 sni 后增加 protocol，共 20 列。
逐包 TSV 末尾保留 `tcp_flags`，并新增 `tcp_flags_text` 显示 SYN、SYN|ACK、FIN|ACK 等。
不能将 HTTP/1.1 原始头部长度和 HTTP/2 压缩帧长度当作同一种测量。
解密模式仅纳入实际解析到相应协议或有服务端 ALPN 证据的连接；
仅有客户端协议提议不算证据，协议未知的连接不会被强行归类。
同一连接如果出现两种协议，将分别保留两行，以 protocol 区分，避免混合长度口径。
`--protocol http1` 或 `--protocol http2` 只筛选协议，不改变三个方向文件的路径或列格式。

HTTP/1.1 长度包括起始请求行/状态行、所有头字段、结尾空行的原始字节，排除正文和 chunked trailers；
定义依据 [RFC 9112 消息格式](https://www.rfc-editor.org/rfc/rfc9112.html#section-2.1)。
实现显式开启 TShark 的 TCP、TLS、HTTP 头部和正文重组，从 PDML 字节范围验证完整性：
重复字段不会重复计数，只有完整覆盖且以 CRLFCRLF 结束的头部才输出。
不完整、冲突的可识别头部显式报错；完全未识别为 HTTP 消息的数据不会伪造头部长度。
HTTP/1.0 本轮不纳入。临时 PDML 在处理完毕后自动关闭删除，结果不保存头字段正文、Cookie 或 URL。

同包多条 HTTP/1.1 消息会产生多个长度，`header` 仍按含头部事件的包计数。
其他包沿用粗分类，`tls_segment` 不等价于解密失败，也不是 HTTP/1 正文包数。
事件按 TShark 解析/重组完成所在的 `frame_no` 排序，同包按消息出现顺序；
因开启正文重组，某些响应事件会落在消息重组完成的包，而不是头部最初到达的包，
因此该排序不能用于声称精确的请求/响应头到达时延。

### HTTP/1.1 重跑结果（2026-10-09）

同一份 Chrome PCAP 和密钥已重跑，当前结果保存于 `data/maintenance_archive/20261012/fetch_forbes_extraction_20261009/sequences/`。

| 范围 | HTTP/1.1 连接数 | 有非空序列的连接数 | 请求头数量 | 响应头数量 |
|---|---:|---:|---:|---:|
| 整份样本 TCP/443 | 12 | 11 | 40 | 40 |
| 福布斯主站及 OSS | 11 | 10 | 38 | 38 |

福布斯另有一条连接只有 HTTP/1.1 协商证据、没有解析到 HTTP 消息，保留空序列。
80 个 HTTP/1.1 长度与独立 TShark 头部行原始字节计数全部一致；此前 HTTP/2 非空连接的各列结果、
不解密三个方向结果均与上轮一致。原 PCAP/密钥 SHA-256 未改变。
当前完整重跑的文件校验见 `data/maintenance_archive/20261012/fetch_forbes_extraction_20261009/validation.json`。

19 项回归测试通过，包含 Chrome/Edge/Firefox 输入格式、HTTP/1 与 HTTP/2 分目录、
多头部、非 ASCII 字节、重复字段和旧中间文件报错，以及真实 TShark 运行的合成 TCP 抓包测试
（跨包头部、同包多消息、Content-Length 正文、chunked 正文/trailer、重传、正文中的伪 HTTP 起始行）。
上述是本地验证范围；后续 Linux 三浏览器实测见下方服务器抽样记录，未执行全量批处理。

### 服务器三浏览器抽样（2026-10-09）

在 8.153.92.147 的 Ubuntu / Python 3.12.14 / TShark 4.2.2 上，抽取
任务 `ubuntu福布斯剩余1-ff75edc9885a2129` 的 `3156-wiki-3156`、`3253-wiki-3253`
两个已完成页面，各取 Chrome、Edge、Firefox，共 6 个真实 PCAP。
最终 6 个全部成功；20 项回归测试通过；六份解密序列均通过独立 TShark 字节长度核对。
24 个原始 PCAP、密钥、状态和完成记录的 SHA-256、大小、修改时间前后一致。

抽样发现 3253 的 Chrome 抓包中，两个不同服务端 IP 的连接都使用客户端端口 35758，
使旧端口文件名冲突。现仅对冲突连接的两个方向增加 `_tcp_<stream-id>` 文件名后缀，
保持无冲突连接原名，双向汇总沿用后缀；不会覆盖其他连接。
当时完全相同端点复用为多个 TCP stream 仍显式报错；该限制已在 2026-10-12 的包级拆分中修复。

最终服务器产物位于两个页面原 PCAP 目录下各自的
`analysis_check_20261009_161955/{chrome,edge,firefox}/`：
`sequences/` 保存六个序列文件，`inputs/<页面目录>/` 保存两套逐包明细。
首轮检查目录 `analysis_check_20261009_161757` 保留失败证据。
未删除抓包、未重启 Worker、未替换服务器运行中的生产源码；验证脚本独立放在
`/root/forbes-analysis-check-20261009/`。本地汇总在 `data/maintenance_archive/20261012/fetch_server_analysis_check_20261009/report.json`。

最新合并版本位于 `data/maintenance_archive/20261012/fetch_forbes_extraction_20261009/sequences/`。
三个解密文件分别包含 25 行（HTTP/1.1 12 行、HTTP/2 13 行），每个文件有 24 行非空序列。
合并前后按 flow 和 protocol 核对，其余全部字段一致；不解密三个文件保持一致。
此前两处测试产物目录已按用户要求清理；新目录从 PCAP 完整重跑，保留两套逐包明细和执行日志。

## 输出口径

不解密每行包含以下 19 列，解密在 sni 后增加 protocol：

```text
id wiki_name flow sni handshake control header data ack tls_segment unknown syn syn_ack fin fin_ack rst rst_ack tcp_other header_lens
```

实际文件使用 TAB 分隔。`id/wiki_name` 沿用 `ID-wiki-名称` 目录名解析规则；
其他目录名会同时作为 id 和名称。每条 TCP 连接单独一行，不按域名合并连接。

| 模式 | 输入目录 | header_lens |
|---|---|---|
| inferred（不解密） | capture_浏览器_inferred | 与旧脚本完全相同：category=header 时尝试取 est_h2_payload_range 按 `-` 分割后的第一项 |
| decrypted（使用密钥分析） | capture_浏览器_flows | 按 h2_frame_type 与 h2_frame_len 对应关系，取所有实际 HEADERS 帧的 payload 长度 |

不解密模式有意保留旧规则，包括多值字符串的处理方式：`61-80,20-30` 得到 `61`，
`61,99` 无法直接转成整数，会跳过。此轮没有修改长度推测算法，也不把推测视为协议真值。
旧算法可能把 HTTP/1.1 加密流量推测为 HTTP/2 HEADERS，因此两种模式不能直接视为等价测量。

HTTP/2 解密模式不会用 TLS 握手长度、HTTP/1 头部长度、DATA 或控制帧长度补充 HEADERS。
一个包携带多个 HEADERS 时全部保留；同包的 HEADERS+DATA 也会保留 HEADERS。
这里的长度是 HTTP/2 帧 payload 长度，不是解压后的 HTTP 头部文本长度；
CONTINUATION 不纳入，也不重组整个 header block。

解密分类在新汇总层重新计算，不使用旧 classify_packets 的宽泛 header 分类。
包级互斥优先级是 HEADERS → DATA → 其他 H2 控制帧 → TLS 握手 → Alert/unknown →
零 TCP payload 按 TCP flags 分类 → tls_segment。
`ack` 仅指零载荷且 flags 恰好为 `0x10` 的纯 ACK。零载荷 SYN、SYN|ACK、FIN、
FIN|ACK、RST、RST|ACK 分别计入 syn、syn_ack、fin、fin_ack、rst、rst_ack；其他非零
组合归 tcp_other，完整标志保留在 tcp_flags_text 中。各类别互斥，SYN|ACK 不再重复
计入 ack，不混入 TLS handshake 或 H2 control。零载荷且 flags 为 0 的包归 unknown。
非零载荷包保持应用层分类，但仍显示原始 TCP 标志。
Chrome、Edge、Firefox、根目录旧入口和 analysis_scripts 入口使用相同规则。

旧逐包 TSV 没有 flags，无法从 `ack` 计数恢复 SYN/FIN/RST。遇到这类零载荷包
或旧 `ack` 行时，汇总明确报错并要求从原始 PCAP 重跑，而不是继续使用旧计数。
需重跑 `infer_packets.py`、`classify_packets.py` 及汇总步骤，勿使用 `--skip-existing`。
当前 `data/maintenance_archive/20261012/fetch_forbes_extraction_20261009` 已于 2026-10-10 从用户提供的 PCAP 重跑，
其 validation.json 记录了具体标志与原始 PCAP 的逐帧核对结果。
ACK 修复不补造 SNI、不删除空 SNI 流，也不改变 Header 长度推测算法。
`header` 是含 HEADERS 的包数，`header_lens` 元素数可以大于包数。
未解析出 H2 的非零 payload 仍归 tls_segment，此分类不等价于“解密失败”。

方向规则：

- forward：客户端到服务端，长度为非负数。
- reverse：服务端到客户端，长度为非负数。
- bidirectional：同一连接上下行按 `frame_no` 合并，上行为正、下行加负号。
  例如 `10,-20,30,-40`；下行零长度保留字符串 `-0`，读取时应先保留符号再转整数。

双向配对使用端点 IP、端口和传输协议，而不是只看域名或文件名。下行没有 SNI 时可继承同连接上行 SNI。
默认缺少对应方向文件就报错，且不会开始写本次结果。若 PCAP 确实只抓到一个方向，
可显式使用 `--allow-unpaired` 合并已观察到的包；每条这种连接会输出警告，
并在 `summary.json` 中记录 `paired=false` 和实际方向，不能据此宣称抓到了双向流量。

## 使用方法

Python 3.10+，安装 TShark，并确保它在 PATH。
先生成中间文件，再汇总（这些命令在仓库根目录执行）：

```bash
python analysis_scripts/batch_process.py /path/to/task
python analysis_scripts/extract_header_seqs.py /path/to/task --browser chrome
python analysis_scripts/extract_header_seqs.py /path/to/task --browser edge
python analysis_scripts/extract_header_seqs.py /path/to/task --browser firefox
```

batch_process 当前只扫描 task 的直接子目录；新汇总入口会递归查找中间文件目录。
缺少密钥时 batch_process 会跳过解密阶段；新入口默认要求两种目录均存在。
可用 `--mode inferred` 只处理不解密结果，不能将它当作解密成功。
建议在新输出目录处理，避免 `--skip-existing` 复用过时或不完整的中间文件。

默认输出结构（浏览器分别保存）：

```text
<root>/header_sequences/chrome/
  inferred/forward.tsv
  inferred/reverse.tsv
  inferred/bidirectional.tsv
  decrypted/forward.tsv
  decrypted/reverse.tsv
  decrypted/bidirectional.tsv
  summary.json
```

可选参数：`--mode inferred|decrypted|both`、
`--direction forward|reverse|bidirectional|all`、`--output-dir DIR`、
`--min-header N`、`--split-sni`、`--allow-unpaired`。
`--split-sni` 输出如 `decrypted/reverse_www.forbeschina.com.tsv`。
`--min-header` 按包数过滤，不按序列元素数过滤。

只处理福布斯域名可在生成中间文件时增加 `--sni-suffix forbeschina.com`；
这会排除广告、统计、CDN 等其他域名。处理整份 PCAP 则不要加该筛选。

单个 PCAP 的完整示例（输出文件夹须先创建，密钥内容无需复制）：

```bash
python analysis_scripts/extract_features.py /path/capture_chrome.pcap --output /out/sample/capture_chrome.tsv
python analysis_scripts/classify_packets.py /path/capture_chrome.pcap /path/tls_keys_chrome.log --flow-tsv /out/sample/capture_chrome.tsv --output-dir /out/sample/capture_chrome_flows
python analysis_scripts/infer_packets.py /path/capture_chrome.pcap --flow-tsv /out/sample/capture_chrome.tsv --output-dir /out/sample/capture_chrome_inferred
python analysis_scripts/extract_header_seqs.py /out --browser chrome --allow-unpaired
```

## 当前范围与保护

- 沿用原管线的 TCP/443 方向约定；包级脚本明确排除 UDP/QUIC 和非 443 流量。
  本次六类输出不代表 PCAP 所有协议；HTTP/3、非标准 TLS 端口尚不支持。
- 旧中间文件只按端口命名。新生成中间文件时检查文件名碰撞及 `tcp.stream`：
  不同端点导致的端口名冲突，以及同端点的多次 TCP 连接，均按 stream 拆分并增加 `_tcp_<stream-id>` 后缀。
  已经被旧版本覆盖或合并的中间文件无法在汇总阶段恢复，需从 PCAP 重新生成。
- 两个包级脚本都会传播 TShark 非零退出，不再将错误输出当作正常空结果。
  不解密脚本显式清空 TShark 的 keylog 配置；应使用原始 PCAP，勿用内嵌解密密钥的 PCAPNG。
- 解密目录存在和空序列都不能单独证明是否解密成功，仍需检查实际协议解析结果。

## 2026-10-09 首轮 HTTP/2 口径样本验证（历史结果）

使用用户提供的 Chrome PCAP（5,052,712 字节）及配套密钥，TShark 4.6.6，Windows：

| 输出 | 流行数 | 不解密非空序列数 | 解密非空序列数 |
|---|---:|---:|---:|
| 上行 | 45 | 13 | 13 |
| 下行 | 33 | 13 | 13 |
| 双向 | 45 | 15 | 13 |

45 条 TCP/443 连接中 33 条有两个方向，12 条只有上行；本次显式启用 allow-unpaired 并记录这些连接。
解密共提取 35 个 HEADERS 长度（上行 18、下行 17），与独立 TShark 导出逐项一致。
不解密上行结果与原 extract_forward_seqs 输出逐行一致，三种方向分类计数守恒。
原始 PCAP、密钥的 SHA-256 在测试前后相同。

样本中 11 条福布斯主站/OSS TLS 连接的服务端 ALPN 都是 HTTP/1.1，10 条解析到了 HTTP/1 消息，
没有 H2 HEADERS。这里解密序列为空符合本轮口径；非空 H2 序列来自其他域名。

首轮测试产物已按用户要求清理；当前完整重跑产物位于 `data/maintenance_archive/20261012/fetch_forbes_extraction_20261009/`。
新增回归测试覆盖三种浏览器文件标签、双向时序、下行零长度、多帧混包、缺向、错误输入及连接碰撞。
首轮真实输入仅验证了这份 Chrome PCAP；后续已完成 Linux 三浏览器六个样本验证，未执行全量批处理。

```bash
python -m unittest discover -s tests -p test_header_sequence_extensions.py -v
```
# 2026-10-12：服务器结果的 inputs 扁平布局

每份浏览器结果中的页面子目录已迁移为以下布局；原始 PCAP 和 TLS 密钥仍留在原采集页面目录。

```text
<页面>/flow_extraction_20261010/<browser>/
  inputs/
    page.json                         # 页面标识与原 PCAP 路径，不包含密钥内容
    capture_<browser>.tsv
    capture_<browser>_flows/
    capture_<browser>_inferred/
  sequences/
    decrypted/{forward,reverse,bidirectional}.tsv
    inferred/{forward,reverse,bidirectional}.tsv
    summary.json
```

`extract_header_seqs.py`、`extract_forward_seqs.py` 从 `inputs/page.json` 读取原页面目录名，继续保留原来的 `id`、`wiki_name`、字段和文件命名，同时兼容旧的 `inputs/<页面>/` 输入。扁平输入缺少页面标识时显式报错，避免把页面编号误写成 `inputs`。

重试脚本默认生成扁平布局。缺少密钥的部分结果仍仅有 inferred 明细与汇总；无可用目标流时不生成虚假的结果目录。`summary.json` 中的明细路径已同步调整，原始采集路径不变。
