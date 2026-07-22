<script setup>
defineProps({ status: { type: String, default: 'UNKNOWN' } })

const labels = {
  CREATED: '待分发', DISPATCHING: '分发中', QUEUED: '排队中', PREPARING: '准备中',
  CAPTURING: '采集中', ANALYZING: '分析中', VALIDATING: '校验中', RUNNING: '运行中',
  CANCELING: '取消中', SUCCEEDED: '成功', PARTIAL: '部分成功', FAILED: '失败',
  CANCELED: '已取消', INTERRUPTED: '已中断', ONLINE: '在线', BUSY: '忙碌',
  OFFLINE: '离线', UNKNOWN: '未探测',
}
</script>

<template>
  <span :class="['status-pill', `status-${status.toLowerCase()}`]">
    <i></i>{{ labels[status] || status }}
  </span>
</template>
