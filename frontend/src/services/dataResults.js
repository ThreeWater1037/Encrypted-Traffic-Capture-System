import { ref } from 'vue'

export const browserNames = { chrome: 'Chrome', edge: 'Edge', firefox: 'Firefox' }
export function fileLabel(files = {}) {
  if (files.pcap === 'invalid' || files.tls === 'invalid') return '文件检查未通过'
  if (files.pcap === 'retired' && files.tls === 'present') return '提取完成 · PCAP 已删除'
  if (files.pcap === 'present' && files.tls === 'present') return 'PCAP + TLS'
  if (files.pcap === 'present') return '缺少 TLS 密钥'
  if (files.tls === 'present') return '缺少 PCAP'
  return '文件未上报'
}

const defaults = () => ({ query: '', browser: '', machine: '', offset: 0, limit: 20 })
export function createDataResults(api) {
  const group = ref('__all'), groupName = ref('全部数据'), options = ref(defaults())
  const rows = ref([]), page = ref({ offset: 0, limit: 20, total: 0 })
  const overview = ref({ stats: { records: 0, urls: 0, batches: 0 }, machines: [] })
  const loading = ref(false), error = ref(''), detail = ref(null), selected = ref(null)
  const detailLoading = ref(false), detailError = ref('')
  let version = 0, detailVersion = 0, controller, detailController
  const pages = new Map(), summaries = new Map(), details = new Map(), views = new Map()
  function remember(cache, key, value, max) {
    cache.delete(key); cache.set(key, value)
    while (cache.size > max) cache.delete(cache.keys().next().value)
  }
  function closeDetail() {
    ++detailVersion; detailController?.abort(); selected.value = null
    detail.value = null; detailError.value = ''; detailLoading.value = false
  }
  function filters() { const { query, browser, machine } = options.value; return { group: group.value, query, browser, machine } }
  async function load(force = false) {
    const current = ++version
    controller?.abort(); controller = new AbortController()
    const signal = controller.signal
    const query = { ...filters(), offset: options.value.offset, limit: options.value.limit }
    const key = JSON.stringify(query), summaryKey = JSON.stringify(filters())
    closeDetail(); error.value = ''
    if (!force && pages.has(key) && summaries.has(summaryKey)) {
      const cached = pages.get(key); rows.value = cached.rows; page.value = cached.page
      overview.value = summaries.get(summaryKey); loading.value = false; return
    }
    loading.value = true; rows.value = []; page.value = { offset: 0, limit: query.limit, total: 0 }
    overview.value = { stats: { records: 0, urls: 0, batches: 0 }, machines: [] }
    try {
      const [result, summary] = await Promise.all([api.getDataResults(query, signal), api.getDataOverview(filters(), signal)])
      if (current !== version) return
      rows.value = result.rows; page.value = result.page; overview.value = summary
      options.value = { ...options.value, offset: result.page.offset }
      remember(pages, JSON.stringify({ ...query, offset: result.page.offset }), result, 8)
      remember(summaries, summaryKey, summary, 12)
    } catch (reason) { if (current === version) error.value = reason.message }
    finally { if (current === version) loading.value = false }
  }
  async function selectGroup(id, name) {
    remember(views, group.value, { ...options.value }, 12)
    group.value = id; groupName.value = name; options.value = { ...(views.get(id) || defaults()) }
    await load()
  }
  async function changeOptions(change) {
    options.value = { ...options.value, offset: 0, ...change }; await load()
  }
  async function openDetail(row) {
    closeDetail(); selected.value = row; const current = ++detailVersion
    const query = { group: group.value, execution_id: row.execution_id }
    const key = JSON.stringify(query)
    if (details.has(key)) { detail.value = details.get(key); return }
    detailController = new AbortController(); detailLoading.value = true
    try {
      const result = await api.getDataResult(query, detailController.signal)
      if (current !== detailVersion) return
      detail.value = result; remember(details, key, result, 20)
    } catch (reason) { if (current === detailVersion) detailError.value = reason.message }
    finally { if (current === detailVersion) detailLoading.value = false }
  }
  function invalidate() { pages.clear(); summaries.clear(); details.clear() }
  function refresh() { invalidate(); return load(true) }
  function dispose() { ++version; controller?.abort(); closeDetail() }
  return { group, groupName, options, rows, page, overview, loading, error, detail, selected, detailLoading,
    detailError, filters, load, selectGroup, changeOptions, openDetail, closeDetail, invalidate, refresh, dispose }
}
