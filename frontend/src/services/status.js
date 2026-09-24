export const statusLabels = {
  ALL: '全部', CREATED: '待分发', DISPATCHING: '分发中', QUEUED: '排队中', PREPARING: '准备中',
  RESUMING: '断点恢复中', CAPTURING: '采集中', CAPTURED: '已采集', ANALYZING: '分析中',
  VALIDATING: '校验中', RUNNING: '运行中', WAITING_FOR_WORKER: '等待 Worker 恢复',
  CANCELING: '取消中', SUCCEEDED: '成功', PARTIAL: '部分成功', FAILED: '失败',
  CANCELED: '已取消', INTERRUPTED: '已中断', ONLINE: '在线', BUSY: '忙碌',
  OFFLINE: '离线', UNKNOWN: '未探测', DONE: '结束', TIMED_OUT: '超时',
}
export const statusLabel = (status) => statusLabels[status] || status || '未知'
export const artifactLabels = {
  pcap: 'PCAP', tls_keylog: 'TLS keylog', checkpoint: '采集完成标记', html: '页面 HTML',
  report: '抓取报告', extract: '提取结果', classify: '分类结果', infer: '推断结果',
}
