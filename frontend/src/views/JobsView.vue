<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Ban, ChevronRight, FileText, Play, RefreshCw, RotateCcw, ScrollText, Trash2 } from '@lucide/vue'
import StatusPill from '../components/StatusPill.vue'
import { statusLabel, artifactLabels } from '../services/status'

const props = defineProps({
  jobs: { type: Array, default: () => [] },
  selectedJob: { type: Object, default: null },
  logs: { type: Array, default: () => [] },
  logsLoading: { type: Boolean, default: false },
  pageLoading: { type: Boolean, default: false },
  refreshing: { type: Boolean, default: false },
  pageOptions: { type: Object, default: () => ({}) },
  selectedJobId: { type: String, default: '' },
  actionBusy: Boolean,
  pageError: { type: String, default: '' },
  listOptions: { type: Object, default: () => ({ status: 'ALL', query: '', limit: 20 }) },
  listPage: { type: Object, default: () => ({ offset: 0, limit: 20, total: 0 }) },
  listLoading: Boolean,
  listError: { type: String, default: '' },
})
const emit = defineEmits(['select', 'cancel', 'resume', 'restart', 'delete', 'logs', 'refresh', 'page', 'list-page'])
const sortOptions = [
  { value: 'created_desc', label: '创建时间：最新在前' },
  { value: 'created_asc', label: '创建时间：最早在前' },
  { value: 'updated_desc', label: '更新时间：最新在前' },
  { value: 'name_asc', label: '任务名称：升序' },
  { value: 'name_desc', label: '任务名称：降序' },
  { value: 'status', label: '任务状态：进行中优先' },
]
const taskSearch = ref(props.listOptions.query || '')
const listPageCount = computed(() => Math.max(1, Math.ceil(props.listPage.total / props.listPage.limit)))
const listPageNumber = computed(() => Math.floor(props.listPage.offset / props.listPage.limit) + 1)
let taskSearchTimer
function searchTasks() {
  clearTimeout(taskSearchTimer)
  taskSearchTimer = setTimeout(() => emit('list-page', { query: taskSearch.value.trim(), offset: 0 }), 300)
}
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
function searchUrls() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => requestPage(), 300)
}
watch(() => props.pageOptions, (options) => {
  clearTimeout(searchTimer)
  urlSearch.value = options.query || ''
  urlStatus.value = options.status || 'ALL'
  pageSize.value = options.limit || 20
})
watch(() => props.selectedJob?.page, () => {
  urlPage.value = Math.floor(pageStart.value / pageSize.value) + 1
}, { immediate: true })
onBeforeUnmount(() => { clearTimeout(searchTimer); clearTimeout(taskSearchTimer) })

function goToPage(event) {
  const value = Number(event.target.value)
  if (Number.isFinite(value)) requestPage(Math.min(pageCount.value, Math.max(1, Math.trunc(value))))
  event.target.value = urlPage.value
}

const terminal = ['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED']
const canCancel = computed(() => props.selectedJob && !terminal.includes(props.selectedJob.status))
const canResume = computed(() => props.selectedJob && ['PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'].includes(props.selectedJob.status))
const canRestart = computed(() => props.selectedJob && terminal.includes(props.selectedJob.status))

function requestResume() {
  if (props.actionBusy || props.pageLoading) return
  if (window.confirm('继续后将沿用原任务目录，已完成 URL 会跳过，未完成 URL 会重新抓取。是否继续？')) {
    emit('resume', props.selectedJob.job_id)
  }
}

function requestRestart() {
  if (props.actionBusy || props.pageLoading) return
  if (window.confirm('新一轮会创建全新任务和输出目录，不复用任何旧检查点。是否创建？')) {
    emit('restart', props.selectedJob.job_id)
  }
}

const deleteCandidate = ref(null)
watch(() => props.selectedJobId, () => { deleteCandidate.value = null })
function requestDelete() {
  if (!canRestart.value || props.actionBusy || props.pageLoading) return
  deleteCandidate.value = { id: props.selectedJob.job_id, name: props.selectedJob.name }
}
function confirmDelete() {
  if (!deleteCandidate.value || props.actionBusy || props.pageLoading || !canRestart.value) return
  const id = deleteCandidate.value.id
  deleteCandidate.value = null
  if (id === props.selectedJobId) emit('delete', id)
}

function displayTime(value) {
  if (!value || Number.isNaN(new Date(value).getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value))
}

function missingChecks(execution) {
  return Object.entries(execution.result?.checks || {}).filter(([, passed]) => !passed).map(([name]) => artifactLabels[name] || name).join('、')
}
const copyMessage = ref('')
async function copyPath(path) {
  try { await navigator.clipboard.writeText(path); copyMessage.value = '路径已复制' }
  catch { copyMessage.value = '无法自动复制，请选中完整路径复制' }
}
</script>

<template>
  <div class="jobs-layout">
    <div v-if="deleteCandidate" class="modal-backdrop" @click.self="deleteCandidate = null" @keydown.esc="deleteCandidate = null">
      <section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="delete-task-title">
        <h3 id="delete-task-title">删除任务“{{ deleteCandidate.name }}”？</h3>
        <p>任务将从列表移除；服务器上的采集文件、日志和检查点全部保留。</p>
        <div class="detail-actions">
          <button class="secondary-button" @click="deleteCandidate = null">返回</button>
          <button class="danger-button" :disabled="actionBusy || pageLoading" @click="confirmDelete">确认删除</button>
        </div>
      </section>
    </div>
    <section class="panel jobs-list-panel">
      <div class="list-toolbar">
        <div><h2>任务记录</h2><p>主控创建的实验及其聚合状态；Worker 直提任务不会自动导入</p></div>
        <button class="icon-button" title="立即刷新任务列表和进度" aria-label="立即刷新任务列表和进度" :disabled="refreshing" @click="emit('refresh')"><RefreshCw :size="15" /></button>
      </div>
      <div class="url-toolbar">
        <input v-model="taskSearch" type="search" aria-label="搜索任务名称或 ID" placeholder="搜索任务名称或 ID" @input="searchTasks" />
        <select :value="listOptions.status" aria-label="任务状态筛选" @change="emit('list-page', { status: $event.target.value, offset: 0 })">
          <option v-for="value in ['ALL', 'CREATED', 'DISPATCHING', 'RUNNING', 'CANCELING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED']" :key="value" :value="value">{{ statusLabel(value) }}</option>
        </select>
        <select :value="listOptions.sort || 'created_desc'" aria-label="任务排序" @change="emit('list-page', { sort: $event.target.value, offset: 0 })">
          <option v-for="option in sortOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </div>
      <p v-if="listError" class="load-error" role="alert">任务列表加载失败，以下可能为旧数据：{{ listError }}</p>
      <p v-if="listLoading" class="update-time">正在加载任务列表…</p>
      <div class="job-list">
        <button v-for="job in jobs" :key="job.job_id" :class="['job-list-item', { active: selectedJobId === job.job_id }]" @click="emit('select', job.job_id)">
          <div class="job-title-row"><strong>{{ job.name }}</strong><StatusPill :status="job.status" /></div>
          <code>{{ job.job_id }}</code>
          <div class="progress-track"><span :style="{ width: `${job.summary.progress}%` }"></span></div>
          <div class="job-meta"><span>{{ job.summary.completed }}/{{ job.summary.total }} 执行单元</span><span>{{ displayTime(job.created_at) }}</span><ChevronRight :size="14" /></div>
        </button>
        <div v-if="!jobs.length && !listLoading" class="empty-state"><FileText :size="30" /><p>暂无符合条件的任务</p></div>
      </div>
      <nav class="url-pagination" aria-label="任务分页">
        <span>共 {{ listPage.total }} 个任务 · {{ listPageNumber }}/{{ listPageCount }} 页</span>
        <div>
          <button class="secondary-button" :disabled="listLoading || listPageNumber <= 1" @click="emit('list-page', { offset: listPage.offset - listPage.limit })">上一页</button>
          <button class="secondary-button" :disabled="listLoading || listPageNumber >= listPageCount" @click="emit('list-page', { offset: listPage.offset + listPage.limit })">下一页</button>
        </div>
      </nav>
    </section>

    <section v-if="selectedJob" class="job-detail">
      <div class="panel detail-hero">
        <div>
          <span class="eyebrow">{{ selectedJob.job_id }}</span>
          <h2>{{ selectedJob.name }}</h2>
          <p>创建于 {{ displayTime(selectedJob.created_at) }} · 阶段 {{ statusLabel(selectedJob.stage) }}</p>
        </div>
        <StatusPill :status="selectedJob.status" />
        <div class="detail-actions">
          <button class="secondary-button" :disabled="logsLoading" @click="emit('logs', selectedJob.job_id)"><ScrollText :size="15" />{{ logsLoading ? '读取中…' : (logs.length ? '刷新日志' : '读取日志') }}</button>
          <button v-if="canResume" :disabled="actionBusy || pageLoading" class="secondary-button" @click="requestResume"><Play :size="15" />从断点继续</button>
          <button v-if="canRestart" :disabled="actionBusy || pageLoading" class="secondary-button" @click="requestRestart"><RotateCcw :size="15" />重新开启新一轮</button>
          <button v-if="canCancel" :disabled="actionBusy || pageLoading || selectedJob.status === 'CANCELING'" class="danger-button" @click="emit('cancel', selectedJob.job_id)"><Ban :size="15" />取消任务</button>
          <button :disabled="!canRestart || actionBusy || pageLoading" class="danger-button" :title="canRestart ? '仅从列表移除，保留服务器资源' : '请先取消任务或等待任务结束'" @click="requestDelete"><Trash2 :size="15" />删除任务</button>
        </div>
      </div>

      <p v-if="actionBusy" class="update-time" role="status">正在处理任务操作…</p>
      <p v-if="selectedJob.error" class="load-error" role="alert">任务异常：{{ selectedJob.error }}</p>
      <p class="update-time">以下按 URL × 机器 × 浏览器的执行单元统计；完成进度包含已处理的失败单元，不代表成功率。</p>
      <div class="metric-grid">
        <div class="metric-card"><span>完成进度</span><strong>{{ selectedJob.summary.progress }}%</strong><small>{{ selectedJob.summary.completed }}/{{ selectedJob.summary.total }}</small></div>
        <div class="metric-card success"><span>成功</span><strong>{{ selectedJob.summary.succeeded }}</strong><small>必选产物通过校验</small></div>
        <div class="metric-card warning"><span>部分成功</span><strong>{{ selectedJob.summary.partial }}</strong><small>产物不全或校验未完成</small></div>
        <div class="metric-card danger"><span>失败</span><strong>{{ selectedJob.summary.failed }}</strong><small>需要检查日志</small></div>
      </div>

      <div v-if="logs.length" class="panel logs-panel">
        <div class="panel-heading"><div><h3>Worker 日志 · 最新 10 行</h3><p>仅在点击“读取日志”或“刷新日志”时更新，每次替换为最新内容</p></div><button class="secondary-button" :disabled="logsLoading" @click="emit('logs', selectedJob.job_id)"><RefreshCw :size="15" />{{ logsLoading ? '读取中…' : '刷新日志' }}</button></div>
        <article v-for="entry in logs" :key="entry.machine_id">
          <div class="log-meta"><strong>{{ entry.machine_name }}</strong><span v-if="!entry.error">本次显示 {{ entry.line_count || 0 }} 行{{ entry.truncated ? ' · 超长日志已截断' : '' }}</span></div>
          <pre>{{ entry.error || entry.text || '暂无日志' }}</pre>
        </article>
      </div>

      <div class="panel result-panel">
        <div class="panel-heading"><div><h3>URL 执行矩阵</h3><p>每一行对应一个 URL，每个卡片对应机器与浏览器组合；状态筛选匹配任一执行单元，“已采集”也包含后续阶段中保留的完成证据</p></div></div>
        <div class="url-toolbar">
          <input v-model="urlSearch" @input="searchUrls" type="search" aria-label="搜索 URL、名称或 ID" placeholder="搜索 URL、名称或 ID" />
          <label>状态<select v-model="urlStatus" @change="requestPage()"><option value="ALL">全部状态</option><option v-for="status in urlStatuses" :key="status" :value="status">{{ statusLabel(status) }}</option></select></label>
          <label>每页<select v-model.number="pageSize" @change="requestPage()"><option :value="20">20 条</option><option :value="50">50 条</option><option :value="100">100 条</option></select></label>
          <span>{{ pageLoading ? '加载中…' : `共 ${totalUrls} 个匹配 URL` }}</span>
        </div>
        <p v-if="pageError" class="load-error" role="alert">本次查询失败，以下结果可能不符合当前筛选：{{ pageError }} <button class="secondary-button" @click="requestPage(urlPage)">重试</button></p>
        <div ref="resultViewport" :aria-busy="pageLoading" class="result-items" tabindex="0" role="region" aria-label="当前页 URL 执行状态">
          <div v-if="!visibleItems.length" class="empty-state"><p>暂无符合条件的 URL</p></div>
          <article v-for="item in visibleItems" :key="item.id" class="result-item">
            <div class="result-url"><StatusPill :status="item.status" /><div><strong>{{ item.name }}</strong><a :href="item.url" target="_blank" rel="noopener noreferrer">{{ item.url }}</a></div></div>
            <div class="execution-grid">
              <div v-for="execution in item.executions" :key="execution.execution_id" class="execution-card">
                <div><strong>{{ execution.machine_name }}</strong><span>{{ execution.browser }}</span></div>
                <StatusPill :status="execution.status" />
                <small>{{ statusLabel(execution.stage) }}</small>
                <p v-if="execution.result?.status === 'CAPTURED' && execution.status !== 'CAPTURED'" class="capture-evidence">已有采集完成证据；当前状态不代表这些产物已丢失，最终校验结果尚未确认。</p>
                <p v-if="execution.error">{{ execution.error }}</p>
                <p v-if="execution.result?.batch_error">批次异常：{{ execution.result.batch_error }}</p>
                <p v-if="missingChecks(execution)">未通过检查：{{ missingChecks(execution) }}</p>
                <div v-for="(artifact, name) in execution.result?.artifacts || {}" :key="name" class="artifact-path">
                  <template v-if="artifact?.path">
                    <span>{{ artifactLabels[name] || name }} · Worker 任务目录内路径</span>
                    <code :title="artifact.path">{{ artifact.path }}</code>
                    <button class="secondary-button" @click="copyPath(artifact.path)">复制路径</button>
                  </template>
                </div>
              </div>
            </div>
          </article>
        </div>
        <p v-if="copyMessage" class="update-time" role="status">{{ copyMessage }}</p>
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

    <section v-else class="panel empty-detail">
      <FileText :size="34" />
      <h2>{{ pageLoading ? '正在加载任务…' : (selectedJobId ? '任务详情未加载' : '选择一个任务') }}</h2>
      <p v-if="selectedJobId">{{ selectedJobId }}</p>
      <p v-if="pageError" class="load-error" role="alert">{{ pageError }}</p>
      <button v-if="selectedJobId && !pageLoading" class="secondary-button" @click="emit('select', selectedJobId)">重试加载</button>
      <p v-else>查看 URL、机器和浏览器级别的实时状态与本地结果路径。</p>
    </section>
  </div>
</template>
