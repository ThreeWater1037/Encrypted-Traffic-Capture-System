export const API_BASE = (import.meta.env.VITE_MASTER_API || 'http://127.0.0.1:5200/api/v1').replace(/\/$/, '')
const MASTER_TOKEN = import.meta.env.VITE_MASTER_TOKEN || ''

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {})
  if (MASTER_TOKEN) headers.set('Authorization', `Bearer ${MASTER_TOKEN}`)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.body instanceof FormData ? 120000 : 30000)
  try {
    const response = await fetch(`${API_BASE}${path}`, { ...options, headers, signal: controller.signal, cache: 'no-store' })
    const data = await response.json().catch((reason) => {
      if (reason.name === 'AbortError') throw reason
      throw new Error(`主控返回了非 JSON 响应（HTTP ${response.status}），请检查服务连接`)
    })
    if (!response.ok) {
      throw new Error(data.message || data.error || `请求失败（HTTP ${response.status}）`)
    }
    return data
  } catch (reason) {
    if (reason.name === 'AbortError') throw new Error(options.method && options.method !== 'GET'
      ? '请求超时，操作结果尚未确认，请刷新查询后再决定是否重试'
      : '请求超时，请检查连接后刷新')
    throw reason
  } finally { clearTimeout(timer) }
}

export const getHealth = () => request('/health')
export const getMachines = () => request('/machines')
export const probeMachine = (machineId) => request(`/machines/${encodeURIComponent(machineId)}/probe`, { method: 'POST' })
export const saveMachine = (payload) => request('/machines', { method: 'POST', body: JSON.stringify(payload) })
export const deleteMachine = (machineId) => request(`/machines/${encodeURIComponent(machineId)}`, { method: 'DELETE' })
export const getJobs = (options = {}) => request(`/jobs?${new URLSearchParams(options)}`)
export const getJob = (jobId, options = {}) => request(`/jobs/${encodeURIComponent(jobId)}?${new URLSearchParams({ unit: 'url', limit: 20, ...options })}`)
export const cancelJob = (jobId) => request(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
export const deleteJob = (jobId) => request(`/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' })
export const resumeJob = (jobId) => request(`/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST' })
export const restartJob = (jobId) => request(`/jobs/${encodeURIComponent(jobId)}/restart`, { method: 'POST', body: JSON.stringify({}) })
export const getJobLogs = (jobId) => {
  const query = new URLSearchParams({ tail_lines: '10', limit: '65536' })
  return request(`/jobs/${encodeURIComponent(jobId)}/logs?${query}`)
}
export const createJsonJob = (payload) => request('/jobs', { method: 'POST', body: JSON.stringify(payload) })
export const createFileJob = (form) => request('/jobs/from-file', { method: 'POST', body: form })
