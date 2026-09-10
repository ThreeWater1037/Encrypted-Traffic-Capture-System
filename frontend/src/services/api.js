const API_BASE = (import.meta.env.VITE_MASTER_API || 'http://127.0.0.1:5200/api/v1').replace(/\/$/, '')
const MASTER_TOKEN = import.meta.env.VITE_MASTER_TOKEN || ''

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {})
  if (MASTER_TOKEN) headers.set('Authorization', `Bearer ${MASTER_TOKEN}`)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers, cache: 'no-store' })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(data.message || data.error || `请求失败（HTTP ${response.status}）`)
  }
  return data
}

export const getHealth = () => request('/health')
export const getMachines = () => request('/machines')
export const probeMachine = (machineId) => request(`/machines/${encodeURIComponent(machineId)}/probe`, { method: 'POST' })
export const saveMachine = (payload) => request('/machines', { method: 'POST', body: JSON.stringify(payload) })
export const deleteMachine = (machineId) => request(`/machines/${encodeURIComponent(machineId)}`, { method: 'DELETE' })
export const getJobs = () => request('/jobs')
export const getJob = (jobId, options = {}) => request(`/jobs/${encodeURIComponent(jobId)}?${new URLSearchParams({ unit: 'url', limit: 20, ...options })}`)
export const cancelJob = (jobId) => request(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
export const resumeJob = (jobId) => request(`/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST' })
export const restartJob = (jobId) => request(`/jobs/${encodeURIComponent(jobId)}/restart`, { method: 'POST', body: JSON.stringify({}) })
export const getJobLogs = (jobId, offsets = {}) => {
  const query = new URLSearchParams({ offsets: JSON.stringify(offsets), limit: '65536' })
  return request(`/jobs/${encodeURIComponent(jobId)}/logs?${query}`)
}
export const createJsonJob = (payload) => request('/jobs', { method: 'POST', body: JSON.stringify(payload) })
export const createFileJob = (form) => request('/jobs/from-file', { method: 'POST', body: form })
