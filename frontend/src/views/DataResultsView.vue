<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Copy, Download, RefreshCw } from '@lucide/vue'
import * as api from '../services/api'
import { browserNames, createDataResults, fileLabel } from '../services/dataResults'
import DataGroupEditor from '../components/DataGroupEditor.vue'

const props = defineProps({ active: Boolean })
const state = createDataResults(api)
const { group, groupName, options, rows, page, overview, loading, error, detail, selected, detailLoading, detailError } = state
const groups = ref([]), groupPage = ref({ offset: 0, limit: 6, total: 0 }), groupQuery = ref('')
const allCount = ref(0), ungroupedCount = ref(0), groupsLoading = ref(false), groupsError = ref('')
const search = ref(''), notice = ref(''), exporting = ref(false), exportError = ref('')
const editor = ref(null), locationOpen = ref(false), locations = ref([]), locationsBusy = ref(false), locationsError = ref('')
const detailPanel = ref(null)
let initialized = false, timer, groupsVersion = 0, groupsController
const pages = computed(() => Math.max(1, Math.ceil(page.value.total / page.value.limit)))
const pageNumber = computed(() => Math.floor(page.value.offset / page.value.limit) + 1)
const machineTotal = computed(() => overview.value.machines.reduce((sum, m) => sum + m.records, 0))
async function loadGroups(offset = 0) {
  const current = ++groupsVersion; groupsController?.abort(); groupsController = new AbortController()
  groupsLoading.value = true; groupsError.value = ''
  try {
    const result = await api.getDataGroups({ offset, limit: 6, query: groupQuery.value }, groupsController.signal)
    if (current !== groupsVersion) return
    groups.value = result.groups; groupPage.value = result.page; allCount.value = result.all_count; ungroupedCount.value = result.ungrouped_count
  } catch (reason) { if (current === groupsVersion) groupsError.value = reason.message }
  finally { if (current === groupsVersion) groupsLoading.value = false }
}
async function chooseGroup(id, name) {
  clearTimeout(timer); notice.value = ''; exportError.value = ''
  const task = state.selectGroup(id, name); search.value = options.value.query; await task
}
function searchUrls() {
  clearTimeout(timer); timer = setTimeout(() => state.changeOptions({ query: search.value.trim() }), 300)
}
function filter(change) { clearTimeout(timer); return state.changeOptions({ query: search.value.trim(), ...change }) }
async function showDetail(row) {
  const task = state.openDetail(row); await nextTick(); detailPanel.value?.scrollIntoView({ block: 'nearest' }); await task
}
async function copy(path) {
  try { await navigator.clipboard.writeText(path); notice.value = '文件位置已复制' }
  catch { notice.value = '无法自动复制，请选中完整路径复制' }
}
async function refresh() {
  clearTimeout(timer); exportError.value = ''; notice.value = ''
  await Promise.all([loadGroups(groupPage.value.offset), state.refresh()])
}
async function exportIndex() {
  if (exporting.value || loading.value || error.value || !page.value.total) return
  // Freeze the current successful filter; later UI changes must not alter this download.
  const { group: selectedGroup, ...filters } = state.filters()
  exporting.value = true; exportError.value = ''; notice.value = ''
  try {
    const blob = await api.exportDataIndex({ group: selectedGroup, filters })
    const url = URL.createObjectURL(blob), anchor = document.createElement('a')
    anchor.href = url; anchor.download = `采集结果索引_${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000)
    notice.value = '索引已导出，包含导出时筛选范围的全部分页'
  } catch (reason) { exportError.value = reason.message }
  finally { exporting.value = false }
}
async function savedGroup(result) {
  editor.value = null; state.invalidate(); groupQuery.value = ''; await loadGroups()
  await chooseGroup(result.id, result.name); notice.value = '总体任务已保存'
}
async function openLocations() {
  locationOpen.value = true; locationsBusy.value = true; locationsError.value = ''
  try { locations.value = (await api.getDataLocations()).machines }
  catch (reason) { locationsError.value = reason.message }
  finally { locationsBusy.value = false }
}
async function saveLocation(machine) {
  locationsBusy.value = true; locationsError.value = ''
  try {
    await api.saveDataLocation({ machine_id: machine.machine_id, root: machine.root })
    state.invalidate(); if (selected.value) await state.openDetail(selected.value)
    notice.value = '机器任务根目录已保存，详情与导出使用相同的位置配置'
  } catch (reason) { locationsError.value = reason.message }
  finally { locationsBusy.value = false }
}
function displayDate(value) { return value ? new Date(value).toLocaleDateString('zh-CN') : '—' }
function fileSize(bytes) { return bytes == null ? '' : bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${bytes.toLocaleString()} B` }
watch(() => props.active, async active => {
  if (active && !initialized) { initialized = true; await Promise.all([loadGroups(), state.load()]) }
}, { immediate: true })
onBeforeUnmount(() => { clearTimeout(timer); groupsController?.abort(); state.dispose() })
</script>

<template>
  <div class="data-results-page">
    <div class="data-section-head"><div><h2>总体任务</h2><p class="data-note">按采集批次归组，查询和定位分析所需文件</p></div><div class="data-inline"><button class="secondary-button" @click="editor = { id: '' }">新建分组</button><button v-if="!group.startsWith('__')" class="secondary-button" @click="editor = { id: group }">整理当前分组</button><button class="secondary-button" :disabled="loading || groupsLoading" @click="refresh"><RefreshCw :size="15" />刷新结果</button></div></div>
    <div class="data-groups">
      <button :class="['data-group', { active: group === '__all' }]" :aria-pressed="group === '__all'" @click="chooseGroup('__all', '全部数据')">全部数据<small>{{ allCount }} 个批次</small></button>
      <button v-for="item in groups" :key="item.id" :class="['data-group', { active: group === item.id }]" :aria-pressed="group === item.id" @click="chooseGroup(item.id, item.name)">{{ item.name }}<small>{{ item.batch_count }} 个批次</small></button>
      <button :class="['data-group', { active: group === '__ungrouped' }]" :aria-pressed="group === '__ungrouped'" @click="chooseGroup('__ungrouped', '未归组')">未归组<small>{{ ungroupedCount }} 个批次</small></button>
    </div>
    <div v-if="groupPage.total > 6 || groupQuery" class="data-group-paging"><form class="data-inline" @submit.prevent="loadGroups()"><input v-model="groupQuery" aria-label="搜索总体任务" placeholder="搜索总体任务" /><button class="secondary-button" :disabled="groupsLoading">查询</button></form><div class="data-inline"><span>共 {{ groupPage.total }} 个分组</span><button class="secondary-button" :disabled="groupsLoading || !groupPage.offset" @click="loadGroups(groupPage.offset - 6)">上一页分组</button><button class="secondary-button" :disabled="groupsLoading || groupPage.offset + 6 >= groupPage.total" @click="loadGroups(groupPage.offset + 6)">下一页分组</button></div></div>
    <p v-if="groupsError" class="load-error" role="alert">分组读取失败：{{ groupsError }}</p>
    <section class="data-results-panel" aria-label="URL 采集结果">
      <div class="data-toolbar"><label class="data-field data-search">跨批次 URL 查询<input v-model="search" type="search" placeholder="输入 URL、域名或来源编号" @input="searchUrls" @keydown.enter="filter({})" /></label><label class="data-field">浏览器<select aria-label="浏览器" :value="options.browser" @change="filter({ browser: $event.target.value })"><option value="">全部浏览器</option><option v-for="(name, key) in browserNames" :key="key" :value="key">{{ name }}</option></select></label><button class="primary-button" :disabled="exporting || loading || !!error || !page.total || search.trim() !== options.query" @click="exportIndex"><Download :size="16" />{{ exporting ? '正在导出…' : '导出索引 CSV' }}</button></div>
      <div class="data-machines" aria-label="机器分布"><span>机器分布</span><button :aria-pressed="!options.machine" :class="{ active: !options.machine }" @click="filter({ machine: '' })">全部机器 · {{ machineTotal }}</button><button v-for="machine in overview.machines" :key="machine.machine_id" :aria-pressed="options.machine === machine.machine_id" :class="{ active: options.machine === machine.machine_id }" @click="filter({ machine: machine.machine_id })">{{ machine.name }} · {{ machine.records }}</button><span>条采集记录</span></div>
      <div class="data-summary"><span>{{ groupName }}<template v-if="!loading && !error"> · {{ overview.stats.urls }} 个 URL · {{ overview.stats.records }} 条记录 · {{ overview.stats.batches }} 个批次</template></span><span class="data-note">每行：一次 URL × 机器 × 浏览器采集</span></div>
      <p v-if="error" class="load-error data-message" role="alert">结果读取失败：{{ error }} <button class="secondary-button" @click="state.load(true)">重试</button></p>
      <p v-else-if="loading" class="data-message" role="status">正在查询结果…</p>
      <div v-else class="data-table-wrap"><table class="data-table"><thead><tr><th>URL</th><th>批次</th><th>机器</th><th>浏览器</th><th>文件</th><th>位置</th></tr></thead><tbody><tr v-for="row in rows" :key="row.execution_id" :class="{ selected: selected?.execution_id === row.execution_id }"><td class="data-url">{{ row.url }}</td><td>{{ row.batch_name }}<small>{{ displayDate(row.created_at) }}</small></td><td>{{ row.machine_name }}</td><td>{{ browserNames[row.browser] || row.browser }}</td><td :class="['present', 'retired'].includes(row.files.pcap) && row.files.tls === 'present' ? 'data-present' : 'data-missing'">{{ fileLabel(row.files) }}</td><td><button class="data-link" @click="showDetail(row)">查看位置</button></td></tr><tr v-if="!rows.length"><td colspan="6" class="data-empty">暂无匹配的采集结果</td></tr></tbody></table></div>
      <div class="data-pagination"><span>{{ page.total ? page.offset + 1 : 0 }}–{{ Math.min(page.offset + page.limit, page.total) }} / {{ page.total }} 条</span><div class="data-inline"><label>每页 <select :value="options.limit" aria-label="每页记录数" @change="filter({ limit: Number($event.target.value) })"><option :value="20">20</option><option :value="50">50</option><option :value="100">100</option></select></label><button class="secondary-button" :disabled="loading || pageNumber <= 1" @click="filter({ offset: page.offset - page.limit })">上一页</button><span>{{ pageNumber }} / {{ pages }}</span><button class="secondary-button" :disabled="loading || pageNumber >= pages" @click="filter({ offset: page.offset + page.limit })">下一页</button></div></div>
    </section>
    <section v-if="selected" ref="detailPanel" class="data-detail" aria-label="完整文件位置"><div class="data-section-head"><h2>完整文件位置</h2><button class="secondary-button" @click="state.closeDetail">收起</button></div><p class="data-url">{{ selected.url }}</p><p v-if="detailLoading" role="status">正在读取文件位置…</p><p v-if="detailError" class="load-error" role="alert">{{ detailError }} <button class="secondary-button" @click="state.openDetail(selected)">重试</button></p><template v-if="detail"><p class="data-note">{{ detail.group_name }} / {{ detail.batch_name }} · {{ browserNames[detail.browser] }}<br />机器：{{ detail.machine_name }}（{{ detail.machine_url }}）<br />Worker 任务目录名：{{ detail.worker_task_id }}</p><div v-for="file in detail.files" :key="file.kind" class="data-file"><div class="data-section-head"><span>{{ file.label }} <small class="data-note">{{ fileSize(file.size) }}</small></span><button v-if="file.path" class="data-link" @click="copy(file.path)"><Copy :size="14" />复制{{ file.absolute ? '完整路径' : '相对路径' }}</button></div><code v-if="file.path" class="data-path">{{ file.path }}</code><p v-else-if="file.state === 'retired'" class="data-note">提取完成，原始 PCAP 已删除</p><p v-else class="data-note">未上报，无可用文件位置</p><span class="data-note">{{ file.source }}<template v-if="file.state === 'invalid'"> · 空文件或检查未通过</template></span></div></template><button class="data-link data-location-entry" @click="openLocations">配置机器任务根目录</button></section>
    <div class="data-footer"><span class="data-note">索引导出包含当前筛选的全部结果及文件位置。</span><button class="data-link" @click="openLocations">机器目录配置</button></div>
    <p v-if="notice" class="data-notice" role="status">{{ notice }}</p><p v-if="exportError" class="load-error" role="alert">索引导出失败：{{ exportError }}</p>
    <DataGroupEditor v-if="editor" :group-id="editor.id" @close="editor = null" @saved="savedGroup" />
    <div v-if="locationOpen" class="modal-backdrop" @keydown.esc="!locationsBusy && (locationOpen = false)"><section class="modal-card data-location-editor" role="dialog" aria-modal="true" aria-labelledby="location-title"><div class="data-section-head"><h2 id="location-title">机器任务根目录</h2><button class="secondary-button" :disabled="locationsBusy" @click="locationOpen = false">关闭</button></div><p class="data-note">填写各 Worker 中存放任务文件夹的绝对目录（例如 /data/worker/tasks），不要附加任务名。历史记录使用已保存的 Worker 目录名拼接；配置推导的路径会标注“未核验”。</p><p v-if="locationsError" class="load-error" role="alert">{{ locationsError }}</p><p v-if="locationsBusy" role="status">正在处理目录配置…</p><div v-for="machine in locations" :key="machine.machine_id" class="data-location-row"><label class="data-field">{{ machine.name }} · {{ machine.base_url }}<input v-model="machine.root" :disabled="locationsBusy" placeholder="尚未配置完整任务根目录" /></label><button class="secondary-button" :disabled="locationsBusy" @click="saveLocation(machine)">保存</button></div></section></div>
  </div>
</template>
