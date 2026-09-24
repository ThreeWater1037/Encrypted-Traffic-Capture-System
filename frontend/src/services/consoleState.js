import { computed, reactive, ref } from 'vue'

// Keep request ownership separate from the last successfully loaded detail.
export function createConsoleState(api) {
  const activeView = ref('workspace')
  const health = ref(null)
  const connectionError = ref('')
  const lastUpdated = ref('')
  const machines = ref([])
  const machinesLoaded = ref(false)
  const jobs = ref([])
  const jobListOptions = ref({ offset: 0, limit: 20, status: 'ALL', query: '', sort: 'created_desc' })
  const jobListPage = ref({ offset: 0, limit: 20, total: 0 })
  const jobListLoading = ref(false)
  const jobListError = ref('')
  const runningCount = ref(0)
  const selectedJobId = ref('')
  const selectedJob = ref(null)
  const jobPageOptions = ref({})
  const jobPageLoading = ref(false)
  const jobPageError = ref('')
  const selectedLogs = ref([])
  const logsLoading = ref(false)
  const loading = ref(false)
  const refreshing = ref(false)
  const message = ref('')
  const error = ref('')
  const pendingActions = reactive({})
  const actionBusy = computed(() => Boolean(pendingActions[selectedJobId.value]))
  let jobVersion = 0
  let listVersion = 0
  let logVersion = 0
  const notify = (text) => { message.value = text; error.value = '' }
  const showError = (reason) => { error.value = reason?.message || String(reason) }

  async function loadHealth() {
    try {
      health.value = await api.getHealth()
      connectionError.value = ''
      lastUpdated.value = new Date().toLocaleString()
    } catch (reason) {
      health.value = null
      connectionError.value = reason.message
      throw reason
    }
  }

  async function loadMachines({ probeUnknown = false } = {}) {
    machines.value = (await api.getMachines()).machines
    machinesLoaded.value = true
    if (probeUnknown) {
      const unknown = machines.value.filter((m) => m.enabled && m.status === 'UNKNOWN')
      if (unknown.length) {
        await Promise.allSettled(unknown.map((m) => api.probeMachine(m.machine_id)))
        machines.value = (await api.getMachines()).machines
      }
    }
  }

  async function loadJobs() {
    const version = ++listVersion
    jobListLoading.value = true
    try {
      const response = await api.getJobs(jobListOptions.value)
      if (version !== listVersion) return
      jobs.value = response.jobs
      jobListPage.value = response.page || { offset: 0, limit: 100, total: response.jobs.length }
      jobListOptions.value = { ...jobListOptions.value, offset: jobListPage.value.offset }
      runningCount.value = response.running_count ?? response.jobs.filter((j) => !['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'].includes(j.status)).length
      jobListError.value = ''
    } catch (reason) {
      if (version === listVersion) jobListError.value = reason.message
      throw reason
    } finally {
      if (version === listVersion) jobListLoading.value = false
    }
  }

  async function refreshSelectedJob(jobId = selectedJobId.value) {
    if (!jobId || jobId !== selectedJobId.value) return
    const version = ++jobVersion
    jobPageLoading.value = true
    try {
      const job = await api.getJob(jobId, jobPageOptions.value)
      if (version === jobVersion && jobId === selectedJobId.value) {
        selectedJob.value = job
        jobPageError.value = ''
      }
    } catch (reason) {
      if (version === jobVersion && jobId === selectedJobId.value) jobPageError.value = reason.message
      throw reason
    } finally {
      if (version === jobVersion) jobPageLoading.value = false
    }
  }

  function selectJob(jobId, receipt = null) {
    if (jobId === selectedJobId.value) return
    selectedJobId.value = jobId
    selectedJob.value = receipt
    jobPageLoading.value = false
    jobPageOptions.value = {}
    jobPageError.value = ''
    selectedLogs.value = []
    logsLoading.value = false
    ++jobVersion
    ++logVersion
  }

  async function handleSelectJob(jobId) {
    selectJob(jobId)
    try { await refreshSelectedJob(jobId) } catch (reason) {
      if (jobId === selectedJobId.value) showError(reason)
    }
  }

  async function handleJobPage(options) {
    jobPageOptions.value = options
    try { await refreshSelectedJob() } catch (reason) { showError(reason) }
  }

  async function handleJobListPage(options) {
    jobListOptions.value = { ...jobListOptions.value, ...options }
    try { await loadJobs() } catch (reason) { showError(reason) }
  }

  async function refreshDashboard(manual = false) {
    if (refreshing.value) return
    refreshing.value = true
    try {
      const results = await Promise.allSettled([loadHealth(), loadMachines(), loadJobs(), refreshSelectedJob()])
      const failed = results.find((result) => result.status === 'rejected')
      if (failed) showError(`刷新未完成，部分数据可能已过期：${failed.reason.message}`)
      else if (manual) notify('已刷新任务进度')
    } finally { refreshing.value = false }
  }

  async function loadInitialData() {
    loading.value = true
    try {
      const results = await Promise.allSettled([loadHealth(), loadMachines({ probeUnknown: true }), loadJobs()])
      const failed = results.find((result) => result.status === 'rejected')
      if (failed) showError(failed.reason)
    } finally { loading.value = false }
  }

  async function refreshAfterMutation(jobId, successText) {
    notify(successText)
    const results = await Promise.allSettled([loadJobs(), refreshSelectedJob(jobId)])
    const failed = results.find((result) => result.status === 'rejected')
    if (failed) showError(`${successText}；刷新失败，请刷新查看，不要重复提交：${failed.reason.message}`)
  }

  async function handleSubmit(submission) {
    if (loading.value) return
    loading.value = true
    try {
      const job = submission.kind === 'file'
        ? await api.createFileJob(submission.form) : await api.createJsonJob(submission.payload)
      selectJob(job.job_id, job)
      activeView.value = 'jobs'
      await refreshAfterMutation(job.job_id, `任务 ${job.job_id} 已创建`)
    } catch (reason) { showError(reason) }
    finally { loading.value = false }
  }

  async function runJobAction(jobId, action) {
    if (pendingActions[jobId]) return
    pendingActions[jobId] = action
    try {
      if (action === 'delete') {
        await api.deleteJob(jobId)
        // Invalidate detail/log/list requests started before the deletion.
        ++listVersion
        jobListLoading.value = false
        jobs.value = jobs.value.filter((job) => job.job_id !== jobId)
        jobListPage.value = { ...jobListPage.value, total: Math.max(0, jobListPage.value.total - 1) }
        if (selectedJobId.value === jobId) selectJob('')
        notify('任务已从列表删除，服务器采集文件、日志和检查点均保留')
        try { await loadJobs() }
        catch (reason) { showError(`任务已删除，列表刷新失败：${reason.message}`) }
      } else if (action === 'restart') {
        const job = await api.restartJob(jobId)
        if (selectedJobId.value === jobId) selectJob(job.job_id, job)
        await refreshAfterMutation(job.job_id, `新一轮任务 ${job.job_id} 已创建`)
      } else {
        await (action === 'resume' ? api.resumeJob(jobId) : api.cancelJob(jobId))
        await refreshAfterMutation(jobId, action === 'resume' ? '已从原任务检查点继续' : '取消请求已发送')
      }
    } catch (reason) { showError(reason) }
    finally { delete pendingActions[jobId] }
  }
  const handleCancel = (id) => runJobAction(id, 'cancel')
  const handleResume = (id) => runJobAction(id, 'resume')
  const handleRestart = (id) => runJobAction(id, 'restart')
  const handleDeleteJob = (id) => runJobAction(id, 'delete')

  async function handleLogs(jobId) {
    if (logsLoading.value || jobId !== selectedJobId.value) return
    const version = ++logVersion
    logsLoading.value = true
    try {
      const response = await api.getJobLogs(jobId)
      if (version === logVersion && jobId === selectedJobId.value) {
        selectedLogs.value = response.logs.map((entry) => entry.error || entry.tail_lines === 10
          ? entry : { ...entry, text: '', error: '日志接口尚未更新，请更新 Master 和 Worker 后重试' })
      }
    } catch (reason) {
      if (version === logVersion) showError(reason)
    } finally { if (version === logVersion) logsLoading.value = false }
  }

  async function handleProbe(machineId) {
    try { await api.probeMachine(machineId); notify('机器能力已刷新') }
    catch (reason) { showError(reason) }
    finally { try { await loadMachines() } catch (reason) { showError(reason) } }
  }

  // The form awaits this promise and keeps its fields on write failure.
  async function handleSaveMachine(payload) {
    await api.saveMachine(payload)
    notify('机器配置已保存')
    try { await loadMachines() }
    catch (reason) { showError(`机器已保存，但列表刷新失败：${reason.message}`) }
  }

  async function handleDeleteMachine(machineId) {
    try {
      await api.deleteMachine(machineId)
      machines.value = machines.value.filter((m) => m.machine_id !== machineId)
      notify('机器已删除')
      try { await loadMachines() } catch (reason) { showError(`机器已删除，但列表刷新失败：${reason.message}`) }
    } catch (reason) { showError(reason) }
  }

  return {
    activeView, health, connectionError, lastUpdated, machines, machinesLoaded, jobs,
    jobListOptions, jobListPage, jobListLoading, jobListError, runningCount,
    selectedJobId, selectedJob, jobPageOptions, jobPageLoading, jobPageError,
    selectedLogs, logsLoading, loading, refreshing, message, error, actionBusy,
    loadInitialData, refreshDashboard, handleSubmit, handleSelectJob, handleJobPage,
    handleJobListPage, handleCancel, handleResume, handleRestart, handleDeleteJob, handleLogs,
    handleProbe, handleSaveMachine, handleDeleteMachine,
  }
}
