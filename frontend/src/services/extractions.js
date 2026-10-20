import { ref } from 'vue'

export const terminalExtraction = status => ['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'].includes(status)
export function createExtractions(api) {
  const machine = ref(''), query = ref(''), candidates = ref([]), jobs = ref([]), detail = ref(null)
  const page = ref({ offset: 0, limit: 20, total: 0 }), queuePage = ref({ offset: 0, limit: 20, total: 0 })
  const loading = ref(false), busy = ref(false), error = ref(''), queueError = ref(''), message = ref('')
  const detailLoading = ref(false)
  const keepPcap = ref(false), retentionMode = ref('first'), keepPcapCount = ref(10)
  let revision = 0, detailRevision = 0
  async function load() {
    if (!machine.value) return
    const rev = ++revision, target = machine.value
    loading.value = true; error.value = ''; queueError.value = ''
    const results = await Promise.allSettled([
      api.getExtractionCandidates({ machine_id: target, query: query.value, offset: page.value.offset, limit: 20 }),
      api.getExtractions(target, { offset: queuePage.value.offset, limit: 20 }),
    ])
    if (rev !== revision) return
    if (results[0].status === 'fulfilled') {
      candidates.value = results[0].value.jobs; page.value = results[0].value.page
    } else { candidates.value = []; error.value = results[0].reason.message }
    if (results[1].status === 'fulfilled') {
      jobs.value = results[1].value.jobs; queuePage.value = results[1].value.page
    } else { jobs.value = []; queueError.value = results[1].reason.message }
    loading.value = false
  }
  async function selectMachine(id) {
    revision++; detailRevision++; machine.value = id; detail.value = null; candidates.value = []; jobs.value = []
    detailLoading.value = false
    page.value = { offset: 0, limit: 20, total: 0 }; queuePage.value = { offset: 0, limit: 20, total: 0 }
    message.value = ''; error.value = ''; queueError.value = ''
    await load()
  }
  async function openDetail(id) {
    const rev = ++detailRevision, target = machine.value
    detailLoading.value = true
    try {
      const value = await api.getExtraction(target, id)
      if (rev === detailRevision && target === machine.value) detail.value = value
    } catch (e) { if (rev === detailRevision) error.value = e.message }
    finally { if (rev === detailRevision) detailLoading.value = false }
  }
  async function submit(jobId, existing = null) {
    if (busy.value) return
    const options = existing
      ? { keep_pcap: existing.keep_pcap ?? false, keep_pcap_count: existing.keep_pcap_count ?? null }
      : { keep_pcap: keepPcap.value, keep_pcap_count: keepPcap.value && retentionMode.value === 'first' ? keepPcapCount.value : null }
    if (options.keep_pcap_count !== null && (!Number.isSafeInteger(options.keep_pcap_count) || options.keep_pcap_count < 1)) {
      error.value = '保留数量必须为正整数'; return
    }
    const target = machine.value
    busy.value = true; error.value = ''; message.value = ''
    try {
      const result = await api.submitExtraction(target, jobId, options)
      if (target !== machine.value) return
      detailRevision++; detail.value = result
      message.value = result.status === 'SUCCEEDED' ? '此任务已提取完成，无需重复执行' : '已加入提取队列，等待本机抓包任务结束后执行'
      await load()
    } catch (e) { if (target === machine.value) error.value = e.message }
    finally { busy.value = false }
  }
  async function cancel(id) {
    if (busy.value) return
    const target = machine.value
    busy.value = true; error.value = ''
    try {
      await api.cancelExtraction(target, id)
      if (target === machine.value) { await load(); await openDetail(id) }
    } catch (e) { if (target === machine.value) error.value = e.message }
    finally { busy.value = false }
  }
  async function refresh() { await load(); if (detail.value) await openDetail(detail.value.task_id) }
  return { machine, query, candidates, jobs, detail, page, queuePage, loading, detailLoading, busy, error, queueError, message,
    keepPcap, retentionMode, keepPcapCount,
    selectMachine, load, openDetail, submit, cancel, refresh }
}
