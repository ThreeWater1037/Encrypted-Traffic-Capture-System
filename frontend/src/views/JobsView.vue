<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Ban, ChevronRight, FileText, Play, RefreshCw, RotateCcw, ScrollText } from '@lucide/vue'
import StatusPill from '../components/StatusPill.vue'

const props = defineProps({
  jobs: { type: Array, default: () => [] },
  selectedJob: { type: Object, default: null },
  logs: { type: Array, default: () => [] },
  logsLoading: { type: Boolean, default: false },
  pageLoading: { type: Boolean, default: false },
  refreshing: { type: Boolean, default: false },
  pageOptions: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['select', 'cancel', 'resume', 'restart', 'logs', 'refresh', 'page'])
const statusFilter = ref('ALL')
const urlSearch = ref(props.pageOptions.query || '')
const urlStatus = ref(props.pageOptions.status || 'ALL')
const pageSize = ref(props.pageOptions.limit || 20)
const urlPage = ref(1)
const resultViewport = ref(null)
const urlItems = computed(() => props.selectedJob?.items || [])
const urlStatuses = ['CREATED', 'DISPATCHING', 'QUEUED', 'PREPARING', 'RESUMING', 'RUNNING', 'CAPTURING', 'CAPTURED', 'ANALYZING', 'VALIDATING', 'WAITING_FOR_WORKER', 'CANCELING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED']
const totalUrls = computed(() => props.selectedJob?.page?.unit === 'url' ? props.selectedJob.page.total : urlItems.value.length)
const pageCount = computed(() => Math.max(1, Math.ceil(totalUrls.value / pageSize.value)))
const pageStart = computed(() => props.selectedJob?.page?.unit === 'url' ? props.selectedJob.page.offset : 0)
const visibleItems = computed(() => urlItems.value.slice(0, pageSize.value))
let searchTimer

function requestPage(page = 1) {
  clearTimeout(searchTimer)
  emit('page', { offset: (page - 1) * pageSize.value, limit: pageSize.value, query: urlSearch.value.trim(), status: urlStatus.value })
  if (resultViewport.value) resultViewport.value.scrollTop = 0
}
watch(urlSearch, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => requestPage(), 300)
})
watch([urlStatus, pageSize], () => requestPage())
watch(() => props.selectedJob?.page, () => {
  urlPage.value = Math.floor(pageStart.value / pageSize.value) + 1
}, { immediate: true })
onBeforeUnmount(() => clearTimeout(searchTimer))

function goToPage(event) {
  const value = Number(event.target.value)
  if (Number.isFinite(value)) requestPage(Math.min(pageCount.value, Math.max(1, Math.trunc(value))))
  event.target.value = urlPage.value
}

const terminal = ['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED']
const filteredJobs = computed(() => statusFilter.value === 'ALL' ? props.jobs : props.jobs.filter((job) => job.status === statusFilter.value))
const canCancel = computed(() => props.selectedJob && !terminal.includes(props.selectedJob.status))
const canResume = computed(() => props.selectedJob && ['PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'].includes(props.selectedJob.status))
const canRestart = computed(() => props.selectedJob && terminal.includes(props.selectedJob.status))
const logElements = new Map()

function setLogElement(machineId, element) {
  if (element) logElements.set(machineId, element)
  else logElements.delete(machineId)
}

function loadedSize(entry) {
  const bytes = Number(entry.next_offset || 0)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}

watch(
  () => props.logs.map((entry) => `${entry.machine_id}:${entry.next_offset}`).join('|'),
  async () => {
    await nextTick()
    for (const element of logElements.values()) element.scrollTop = element.scrollHeight
  },
)

function requestResume() {
  if (window.confirm('继续后将沿用原任务目录，已完成 URL 会跳过，未完成 URL 会重新抓取。是否继续？')) {
    emit('resume', props.selectedJob.job_id)
  }
}

function requestRestart() {
  if (window.confirm('新一轮会创建全新任务和输出目录，不复用任何旧检查点。是否创建？')) {
    emit('restart', props.selectedJob.job_id)
  }
}

function displayTime(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value))
}
</script>

<template>
  <div class="jobs-layout">
    <section class="panel jobs-list-panel">
      <div class="list-toolbar">
        <div><h2>任务记录</h2><p>主控创建的实验及其聚合状态；Worker 直提任务不会自动导入</p></div>
        <button class="icon-button" title="立即刷新任务列表、进度和日志" aria-label="立即刷新任务列表、进度和日志" :disabled="refreshing" @click="emit('refresh')"><RefreshCw :size="15" /></button>
      </div>
      <div class="filter-row">
        <button v-for="value in ['ALL', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED']" :key="value" :class="{ active: statusFilter === value }" @click="statusFilter = value">{{ value }}</button>
      </div>
      <div class="job-list">
        <button v-for="job in filteredJobs" :key="job.job_id" :class="['job-list-item', { active: selectedJob?.job_id === job.job_id }]" @click="emit('select', job.job_id)">
          <div class="job-title-row"><strong>{{ job.name }}</strong><StatusPill :status="job.status" /></div>
          <code>{{ job.job_id }}</code>
          <div class="progress-track"><span :style="{ width: `${job.summary.progress}%` }"></span></div>
          <div class="job-meta"><span>{{ job.summary.completed }}/{{ job.summary.total }} 执行单元</span><span>{{ displayTime(job.created_at) }}</span><ChevronRight :size="14" /></div>
        </button>
        <div v-if="!filteredJobs.length" class="empty-state"><FileText :size="30" /><p>暂无符合条件的任务</p></div>
      </div>
    </section>

    <section v-if="selectedJob" class="job-detail">
      <div class="panel detail-hero">
        <div>
          <span class="eyebrow">{{ selectedJob.job_id }}</span>
          <h2>{{ selectedJob.name }}</h2>
          <p>创建于 {{ displayTime(selectedJob.created_at) }} · 阶段 {{ selectedJob.stage }}</p>
        </div>
        <StatusPill :status="selectedJob.status" />
        <div class="detail-actions">
          <button class="secondary-button" :disabled="logsLoading" @click="emit('logs', selectedJob.job_id)"><ScrollText :size="15" />{{ logsLoading ? '读取中…' : (logs.length ? '刷新日志' : '读取日志') }}</button>
          <button v-if="canResume" class="secondary-button" @click="requestResume"><Play :size="15" />从断点继续</button>
          <button v-if="canRestart" class="secondary-button" @click="requestRestart"><RotateCcw :size="15" />重新开启新一轮</button>
          <button v-if="canCancel" class="danger-button" @click="emit('cancel', selectedJob.job_id)"><Ban :size="15" />取消任务</button>
        </div>
      </div>

      <div class="metric-grid">
        <div class="metric-card"><span>完成进度</span><strong>{{ selectedJob.summary.progress }}%</strong><small>{{ selectedJob.summary.completed }}/{{ selectedJob.summary.total }}</small></div>
        <div class="metric-card success"><span>成功</span><strong>{{ selectedJob.summary.succeeded }}</strong><small>完整产物</small></div>
        <div class="metric-card warning"><span>部分成功</span><strong>{{ selectedJob.summary.partial }}</strong><small>部分产物缺失</small></div>
        <div class="metric-card danger"><span>失败</span><strong>{{ selectedJob.summary.failed }}</strong><small>需要检查日志</small></div>
      </div>

      <div v-if="logs.length" class="panel logs-panel">
        <div class="panel-heading"><div><h3>Worker 日志</h3><p>每 5 分钟自动更新；点击“立即刷新”或“刷新日志”可随时读取最新日志</p></div></div>
        <article v-for="entry in logs" :key="entry.machine_id">
          <div class="log-meta"><strong>{{ entry.machine_name }}</strong><span>已读取 {{ loadedSize(entry) }} · {{ entry.eof ? '已追上最新日志' : '继续加载中' }}</span></div>
          <pre :ref="(element) => setLogElement(entry.machine_id, element)">{{ entry.error || entry.text || '暂无日志' }}</pre>
        </article>
      </div>

      <div class="panel result-panel">
        <div class="panel-heading"><div><h3>URL 执行矩阵</h3><p>每一行对应一个 URL，每个卡片对应机器与浏览器组合；按 URL 分页加载，状态筛选匹配任一执行单元</p></div></div>
        <div class="url-toolbar">
          <input v-model="urlSearch" type="search" aria-label="搜索 URL、名称或 ID" placeholder="搜索 URL、名称或 ID" />
          <label>状态<select v-model="urlStatus"><option value="ALL">全部状态</option><option v-for="status in urlStatuses" :key="status" :value="status">{{ status }}</option></select></label>
          <label>每页<select v-model.number="pageSize"><option :value="20">20 条</option><option :value="50">50 条</option><option :value="100">100 条</option></select></label>
          <span>{{ pageLoading ? '加载中…' : `共 ${totalUrls} 个匹配 URL` }}</span>
        </div>
        <div ref="resultViewport" :aria-busy="pageLoading" class="result-items" tabindex="0" role="region" aria-label="当前页 URL 执行状态">
          <div v-if="!visibleItems.length" class="empty-state"><p>暂无符合条件的 URL</p></div>
          <article v-for="item in visibleItems" :key="item.id" class="result-item">
            <div class="result-url"><StatusPill :status="item.status" /><div><strong>{{ item.name }}</strong><a :href="item.url" target="_blank">{{ item.url }}</a></div></div>
            <div class="execution-grid">
              <div v-for="execution in item.executions" :key="execution.execution_id" class="execution-card">
                <div><strong>{{ execution.machine_name }}</strong><span>{{ execution.browser }}</span></div>
                <StatusPill :status="execution.status" />
                <small>{{ execution.stage }}</small>
                <p v-if="execution.error">{{ execution.error }}</p>
                <code v-if="execution.result?.artifacts?.pcap?.path">PCAP · {{ execution.result.artifacts.pcap.path }}</code>
                <code v-if="execution.result?.artifacts?.tls_keylog?.path">TLS keylog · {{ execution.result.artifacts.tls_keylog.path }}</code>
              </div>
            </div>
          </article>
        </div>
        <nav class="url-pagination" aria-label="URL 分页">
          <span>第 {{ totalUrls ? pageStart + 1 : 0 }}–{{ Math.min(pageStart + pageSize, totalUrls) }} 条 / 共 {{ totalUrls }} 条</span>
          <div>
            <button class="secondary-button" :disabled="pageLoading || urlPage === 1" @click="requestPage(1)">首页</button>
            <button class="secondary-button" :disabled="pageLoading || urlPage === 1" @click="requestPage(urlPage - 1)">上一页</button>
            <label>第 <input :value="urlPage" type="number" min="1" :max="pageCount" aria-label="跳转到 URL 页码" @change="goToPage" /> / {{ pageCount }} 页</label>
            <button class="secondary-button" :disabled="pageLoading || urlPage === pageCount" @click="requestPage(urlPage + 1)">下一页</button>
            <button class="secondary-button" :disabled="pageLoading || urlPage === pageCount" @click="requestPage(pageCount)">末页</button>
          </div>
        </nav>
      </div>

    </section>

    <section v-else class="panel empty-detail"><FileText :size="34" /><h2>选择一个任务</h2><p>查看 URL、机器和浏览器级别的实时状态与本地结果路径。</p></section>
  </div>
</template>
