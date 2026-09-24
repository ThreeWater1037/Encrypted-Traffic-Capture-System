<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { FileUp, Globe2, Play, Plus, RefreshCw, Server, ShieldCheck, Trash2 } from '@lucide/vue'
import StatusPill from '../components/StatusPill.vue'
import { validTargets, parseTableItems } from '../services/workspace'

const props = defineProps({ machines: { type: Array, default: () => [] }, machinesLoaded: Boolean, loading: Boolean })
const emit = defineEmits(['submit', 'probe'])

const DRAFT_KEY = 'flowlab.workspace.draft.v2'
const defaultRows = [
  { id: '1', name: 'Example', url: 'https://example.com/' },
  { id: '2', name: 'Wikipedia', url: 'https://zh.wikipedia.org/wiki/钦东高速公路' },
]

function loadDraft() {
  try {
    const value = JSON.parse(window.localStorage.getItem(DRAFT_KEY) || 'null')
    return value && typeof value === 'object' ? value : {}
  } catch {
    return {}
  }
}

const draft = loadDraft()
const restoredRows = Array.isArray(draft.inputRows) && draft.inputRows.length
  ? draft.inputRows.filter((row) => row && typeof row === 'object').map((row) => ({
      id: typeof row.id === 'string' ? row.id : '',
      name: typeof row.name === 'string' ? row.name : '',
      url: typeof row.url === 'string' ? row.url : '',
    }))
  : defaultRows

const mode = ref('text')
const jobName = ref(typeof draft.jobName === 'string' ? draft.jobName : '本机流量采集实验')
let inputRowKey = restoredRows.length
const inputRows = ref(restoredRows.map((row, index) => ({ key: index + 1, ...row })))
const selectedFile = ref(null)
const pcap = ref(typeof draft.pcap === 'boolean' ? draft.pcap : true)
const saveHtml = ref(typeof draft.saveHtml === 'boolean' ? draft.saveHtml : false)
const saveReports = ref(typeof draft.saveReports === 'boolean' ? draft.saveReports : false)
const steps = reactive({ extract: false, classify: false, infer: false, ...(draft.steps || {}) })
const withCoframe = ref(typeof draft.withCoframe === 'boolean' ? draft.withCoframe : false)
const sniSuffixes = ref(typeof draft.sniSuffixes === 'string' ? draft.sniSuffixes : '')
const selections = reactive(draft.selections && typeof draft.selections === 'object' ? draft.selections : {})
const localError = ref('')

const enabledMachines = computed(() => props.machines.filter((machine) => machine.enabled))
const selectedCount = computed(() => validTargets(props.machines, selections).length)

function browserNames(machine) {
  return machine.capabilities?.browsers?.map((browser) => browser.name) || []
}

let selectionsInitialized = false
watch([() => props.machines, () => props.machinesLoaded], ([machines, loaded]) => {
  if (!loaded) return
  const targets = validTargets(machines, selections)
  for (const id of Object.keys(selections)) delete selections[id]
  for (const target of targets) selections[target.machine_id] = target.browsers
  if (!selectionsInitialized && !targets.length) {
    const first = machines.find((machine) => machine.enabled && ['ONLINE', 'BUSY'].includes(machine.status) && browserNames(machine).length)
    if (first) selections[first.machine_id] = [browserNames(first).includes('chrome') ? 'chrome' : browserNames(first)[0]]
  }
  selectionsInitialized = true
}, { immediate: true, deep: true })

// 保存可序列化的工作台草稿；浏览器禁止恢复本地文件选择，因此不保存 selectedFile。
watch(
  [jobName, inputRows, pcap, saveHtml, saveReports, steps, withCoframe, sniSuffixes, selections],
  () => {
    try {
      window.localStorage.setItem(DRAFT_KEY, JSON.stringify({
        jobName: jobName.value,
        inputRows: inputRows.value.map(({ id, name, url }) => ({ id, name, url })),
        pcap: pcap.value,
        saveHtml: saveHtml.value,
        saveReports: saveReports.value,
        steps: { ...steps },
        withCoframe: withCoframe.value,
        sniSuffixes: sniSuffixes.value,
        selections: { ...selections },
      }))
    } catch {
      // localStorage 被浏览器策略禁用时，组件常驻仍可保证页面切换不丢数据。
    }
  },
  { deep: true },
)

function toggleBrowser(machineId, browser) {
  const values = selections[machineId] || []
  selections[machineId] = values.includes(browser)
    ? values.filter((value) => value !== browser)
    : [...values, browser]
}

function nextRowId() {
  const used = new Set(inputRows.value.map((row) => row.id.trim()))
  let candidate = inputRows.value.length + 1
  while (used.has(String(candidate))) candidate += 1
  return String(candidate)
}

function addRow() {
  inputRowKey += 1
  inputRows.value.push({ key: inputRowKey, id: nextRowId(), name: '', url: '' })
}

function removeRow(index) {
  if (inputRows.value.length === 1) {
    inputRows.value[0] = { ...inputRows.value[0], id: '1', name: '', url: '' }
    return
  }
  inputRows.value.splice(index, 1)
}

function targetPayload() {
  return validTargets(props.machines, selections)
}

function submit() {
  localError.value = ''
  if (props.loading) return
  try {
    if (!props.machinesLoaded) throw new Error('机器列表尚未加载成功，请刷新后重试')
    if (!jobName.value.trim()) throw new Error('请填写任务名称')
    const targets = targetPayload()
    if (!targets.length) throw new Error('请至少选择一台机器和一个浏览器')
    const analysis = {
      steps: pcap.value ? Object.entries(steps).filter(([, enabled]) => enabled).map(([name]) => name) : [],
      with_coframe: pcap.value && steps.infer && withCoframe.value,
      sni_suffixes: sniSuffixes.value.split(',').map((value) => value.trim()).filter(Boolean),
    }
    const outputs = { html: saveHtml.value, reports: saveReports.value }
    if (mode.value === 'file') {
      if (!selectedFile.value) throw new Error('请选择 UTF-8 编码的 TXT/TSV 文件')
      const form = new FormData()
      form.append('name', jobName.value)
      form.append('file', selectedFile.value)
      form.append('targets', JSON.stringify(targets))
      form.append('pcap', String(pcap.value))
      form.append('save_html', String(outputs.html))
      form.append('save_reports', String(outputs.reports))
      form.append('analysis_steps', analysis.steps.join(','))
      form.append('with_coframe', String(analysis.with_coframe))
      form.append('sni_suffixes', analysis.sni_suffixes.join(','))
      emit('submit', { kind: 'file', form })
      return
    }
    const items = parseTableItems(inputRows.value)
    if (!items.length) throw new Error('请输入至少一个 URL')
    emit('submit', { kind: 'json', payload: { name: jobName.value, items, targets, pcap: pcap.value, outputs, analysis } })
  } catch (reason) {
    localError.value = reason.message
  }
}
</script>

<template>
  <div class="workspace-grid">
    <section class="panel composer-panel">
      <div class="section-heading">
        <div><span class="step-number">01</span><div><h2>准备实验输入</h2><p>少量数据使用表格录入，大批量数据上传 TXT/TSV</p></div></div>
        <div class="segmented"><button :class="{ active: mode === 'text' }" @click="mode = 'text'">表格录入</button><button :class="{ active: mode === 'file' }" @click="mode = 'file'">上传文件</button></div>
      </div>
      <label class="field"><span>任务名称</span><input v-model="jobName" maxlength="120" /></label>
      <div v-if="mode === 'text'" class="input-table-wrap">
        <div class="input-table-head"><span>ID</span><span>名称</span><span>完整 URL</span><span>操作</span></div>
        <div v-for="(row, index) in inputRows" :key="row.key" class="input-table-row">
          <input v-model="row.id" maxlength="64" :aria-label="`第 ${index + 1} 行 ID`" placeholder="1" />
          <input v-model="row.name" maxlength="80" :aria-label="`第 ${index + 1} 行名称`" placeholder="例如 Wikipedia" />
          <input v-model="row.url" maxlength="2048" :aria-label="`第 ${index + 1} 行 URL`" placeholder="https://example.com/" />
          <button class="delete-row" :title="`删除第 ${index + 1} 行`" @click="removeRow(index)"><Trash2 :size="14" /></button>
        </div>
        <button class="add-row" @click="addRow"><Plus :size="14" />添加一行</button>
        <small class="table-tip">空白行会自动忽略；提交前会检查必填项、重复 ID 和 URL 格式。</small>
      </div>
      <label v-else class="upload-zone">
        <FileUp :size="28" /><strong>{{ selectedFile?.name || '选择 TXT / TSV 文件' }}</strong><small>UTF-8 编码，每行：ID〈Tab〉名称〈Tab〉完整 URL；以 # 开头的行为注释</small>
        <input type="file" accept=".txt,.tsv,text/plain,text/tab-separated-values" @change="selectedFile = $event.target.files[0]" />
      </label>
      <p v-if="mode === 'file'" class="table-tip">示例：1〈Tab〉Example〈Tab〉https://example.com/。请使用真正的制表符，大小与条数上限由主控配置。</p>
    </section>

    <section class="panel target-panel">
      <div class="section-heading">
        <div><span class="step-number">02</span><div><h2>选择机器与浏览器</h2><p>仅显示 Worker 实际探测到的能力</p></div></div>
        <span class="selection-count">已选 {{ selectedCount }} 台</span>
      </div>
      <div class="target-list">
        <article v-for="machine in enabledMachines" :key="machine.machine_id" class="target-card">
          <div class="target-main">
            <span class="machine-icon"><Server :size="19" /></span>
            <div><strong>{{ machine.name }}</strong><small>{{ machine.base_url }}</small></div>
            <StatusPill :status="machine.status" />
          </div>
          <div v-if="browserNames(machine).length" class="browser-options">
            <button v-for="browser in browserNames(machine)" :key="browser" :class="{ selected: selections[machine.machine_id]?.includes(browser) }" @click="toggleBrowser(machine.machine_id, browser)">
              <span>{{ browser }}</span><ShieldCheck :size="13" />
            </button>
          </div>
          <button v-else class="probe-link" @click="emit('probe', machine.machine_id)"><RefreshCw :size="13" /> 探测 Worker 能力</button>
        </article>
        <div v-if="!enabledMachines.length" class="empty-state"><Server :size="28" /><p>尚未配置可用 Worker</p></div>
      </div>
    </section>

    <section class="panel options-panel">
      <div class="section-heading"><div><span class="step-number">03</span><div><h2>采集与可选产物</h2><p>默认只保留 PCAP 与 TLS keylog；所有访问均使用全新 Profile</p></div></div></div>
      <div class="option-row"><div><strong>抓取 PCAP</strong><small>TLS keylog 固定保存；PCAP 默认开启</small></div><label class="switch"><input v-model="pcap" type="checkbox" /><span></span></label></div>
      <div class="analysis-steps">
        <span>其他抓取产物（可选）</span>
        <label><input v-model="saveHtml" type="checkbox" />页面 HTML</label>
        <label><input v-model="saveReports" type="checkbox" />抓取报告</label>
      </div>
      <div class="analysis-steps">
        <span>后处理流水线（可选）</span>
        <label v-for="label in ['extract', 'classify', 'infer']" :key="label"><input v-model="steps[label]" :disabled="!pcap" type="checkbox" />{{ label }}</label>
      </div>
      <div class="options-two">
        <label class="field"><span>SNI 后缀过滤（逗号分隔）</span><input v-model="sniSuffixes" placeholder="留空表示不过滤" /></label>
        <label class="check-line"><input v-model="withCoframe" :disabled="!pcap || !steps.infer" type="checkbox" />启用 coframe 分析</label>
      </div>
      <div class="no-cache-note"><ShieldCheck :size="16" /><div><strong>无缓存模式已固定启用</strong><small>主控和 Worker 均返回 no-store；每次实验创建全新任务目录与浏览器配置。</small></div></div>
    </section>

    <footer class="submit-bar">
      <div><Globe2 :size="18" /><span>任务将通过主控分发，实验文件保存在各 Worker 本地</span></div>
      <span v-if="localError" class="inline-error">{{ localError }}</span>
      <button class="primary-button" :disabled="loading || !machinesLoaded" @click="submit"><Play :size="16" />{{ loading ? '提交中…' : '创建实验任务' }}</button>
    </footer>
  </div>
</template>
