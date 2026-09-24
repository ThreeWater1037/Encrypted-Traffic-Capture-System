import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import * as vue from 'vue'
import { createConsoleState } from '../src/services/consoleState.js'
import { validTargets, parseTableItems } from '../src/services/workspace.js'
import { statusLabel } from '../src/services/status.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const api = (overrides = {}) => ({
  getHealth: async () => ({ status: 'ok' }), getMachines: async () => ({ machines: [] }),
  getJobs: async () => ({ jobs: [], page: { offset: 0, limit: 20, total: 0 }, running_count: 0 }),
  getJob: async (id) => ({ job_id: id, status: 'FAILED' }),
  cancelJob: async () => ({}), resumeJob: async () => ({}), saveMachine: async () => ({}),
  ...overrides,
})

test('creation receipt survives list refresh failure; one write only', async () => {
  let writes = 0
  const state = createConsoleState(api({ createJsonJob: async () => ({ job_id: `created-${++writes}` }),
    getJobs: async () => { throw new Error('offline') } }))
  await state.handleSubmit({ kind: 'json', payload: {} })
  assert.equal(writes, 1)
  assert.equal(state.selectedJobId.value, 'created-1')
  assert.equal(state.activeView.value, 'jobs')
  assert.match(state.message.value, /已创建/)
  assert.match(state.error.value, /不要重复提交/)
})

test('submit and restart are locked until their first request completes', async () => {
  for (const action of ['submit', 'restart']) {
    let writes = 0
    const response = deferred()
    const write = () => { ++writes; return response.promise }
    const state = createConsoleState(api({ createJsonJob: write, restartJob: write }))
    await state.handleSelectJob('old')
    const run = () => action === 'submit' ? state.handleSubmit({ kind: 'json', payload: {} }) : state.handleRestart('old')
    const first = run(); const second = run()
    assert.equal(writes, 1)
    response.resolve({ job_id: 'new' })
    await Promise.all([first, second])
    assert.equal(state.selectedJobId.value, 'new')
  }
})

test('refresh targets the newly selected ID while its detail is pending', async () => {
  const response = deferred()
  const requested = []
  const state = createConsoleState(api({ getJob: (id) => {
    requested.push(id)
    return id === 'B' ? response.promise : Promise.resolve({ job_id: id })
  } }))
  await state.handleSelectJob('A')
  const select = state.handleSelectJob('B')
  const refresh = state.refreshDashboard()
  response.resolve({ job_id: 'B' })
  await Promise.all([select, refresh])
  assert.deepEqual(requested, ['A', 'B', 'B'])
  assert.equal(state.selectedJob.value.job_id, 'B')
})

test('late cancel, resume and restart replies cannot replace another selected job', async () => {
  for (const action of ['Cancel', 'Resume', 'Restart']) {
    const response = deferred()
    const state = createConsoleState(api({ [`${action.toLowerCase()}Job`]: () => response.promise }))
    await state.handleSelectJob('A')
    const operation = state[`handle${action}`]('A')
    await state.handleSelectJob('B')
    response.resolve({ job_id: 'new' })
    await operation
    assert.equal(state.selectedJob.value.job_id, 'B')
  }
})

test('out of order task details never paint the old task', async () => {
  const response = deferred()
  const state = createConsoleState(api({ getJob: (id) => id === 'A' ? response.promise : Promise.resolve({ job_id: id }) }))
  const first = state.handleSelectJob('A')
  await state.handleSelectJob('B')
  response.resolve({ job_id: 'A' }); await first
  assert.equal(state.selectedJob.value.job_id, 'B')
})

test('failed URL filter exposes stale results; successful retry clears the error', async () => {
  let failed = false
  const state = createConsoleState(api({ getJob: async (id) => {
    if (failed) throw new Error('query failed')
    return { job_id: id }
  } }))
  await state.handleSelectJob('A')
  failed = true
  await state.handleJobPage({ status: 'FAILED', offset: 0, limit: 20 })
  assert.equal(state.jobPageError.value, 'query failed')
  assert.equal(state.selectedJob.value.job_id, 'A')
  failed = false
  await state.handleJobPage({ status: 'FAILED', offset: 0, limit: 20 })
  assert.equal(state.jobPageError.value, '')
})

test('health turns offline after a previous success', async () => {
  let offline = false
  const state = createConsoleState(api({ getHealth: async () => {
    if (offline) throw new Error('offline')
    return { status: 'ok' }
  } }))
  await state.loadInitialData()
  assert.ok(state.health.value)
  offline = true; await state.refreshDashboard()
  assert.equal(state.health.value, null)
  assert.equal(state.connectionError.value, 'offline')
  assert.ok(state.lastUpdated.value)
})

test('task list filters and pagination survive selection and global count is used', async () => {
  const calls = []
  const state = createConsoleState(api({ getJobs: async (options) => {
    calls.push(options)
    return { jobs: [{ job_id: 'terminal', status: 'FAILED' }], page: { offset: options.offset, limit: 20, total: 123 }, running_count: 4 }
  } }))
  await state.handleJobListPage({ status: 'FAILED', query: 'experiment', offset: 100 })
  await state.handleSelectJob('A'); await state.handleSelectJob('B')
  assert.equal(state.jobListOptions.value.status, 'FAILED')
  assert.equal(state.jobListPage.value.offset, 100)
  assert.equal(state.runningCount.value, 4)
  assert.equal(calls[0].query, 'experiment')
})

test('late list response cannot replace a newer filter', async () => {
  const response = deferred()
  const state = createConsoleState(api({ getJobs: (options) => options.status === 'FAILED' ? response.promise : Promise.resolve({ jobs: [{ job_id: 'success' }], page: { offset: 0, total: 1, limit: 20 }, running_count: 0 }) }))
  const first = state.handleJobListPage({ status: 'FAILED' })
  await state.handleJobListPage({ status: 'SUCCEEDED' })
  response.resolve({ jobs: [{ job_id: 'failed' }] }); await first
  assert.equal(state.jobs.value[0].job_id, 'success')
})

test('log reply belongs to the selected task, logs remain manual', async () => {
  const response = deferred(); let reads = 0
  const state = createConsoleState(api({ getJobLogs: () => { reads++; return response.promise } }))
  await state.handleSelectJob('A')
  const logs = state.handleLogs('A')
  await state.handleSelectJob('B'); await state.refreshDashboard()
  response.resolve({ logs: [{ tail_lines: 10, text: 'A log' }] }); await logs
  assert.deepEqual(state.selectedLogs.value, [])
  assert.equal(reads, 1)
})

test('deleted, disabled or unsupported machine selections are excluded', () => {
  const machines = [{ machine_id: 'disabled', enabled: false }, { machine_id: 'live', enabled: true, capabilities: { browsers: [{ name: 'chrome' }] } }]
  assert.deepEqual(validTargets(machines, { deleted: ['chrome'], disabled: ['chrome'], live: ['chrome', 'edge'] }), [{ machine_id: 'live', browsers: ['chrome'] }])
  assert.deepEqual(validTargets(machines, { live: 'invalid saved draft' }), [])
})

test('table validation rejects invalid IDs, duplicate IDs and authenticated URLs', () => {
  const good = { id: '1', name: 'Example', url: 'https://example.com/' }
  assert.deepEqual(parseTableItems([good]), [good])
  assert.throws(() => parseTableItems([{ ...good, id: '中文' }]), /ID/)
  assert.throws(() => parseTableItems([good, good]), /重复/)
  assert.throws(() => parseTableItems([{ ...good, url: 'https://user:pass@example.com/' }]), /用户名/)
  assert.equal(statusLabel('CAPTURED'), '已采集')
})

// Exercise the actual form script with Vue's reactivity; parent writes are awaited.
function machineForm(saveMachine) {
  const source = fs.readFileSync(new URL('../src/views/MachinesView.vue', import.meta.url), 'utf8')
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import[^\n]*\n/gm, '')
  const env = { ...vue, defineProps: () => ({ saveMachine }), defineEmits: () => () => {} }
  return new Function('env', `with(env){${script};return {openAddForm, form, submit, showForm, saving, formError}}`)(env)
}

test('machine form stays open and retains fields after write failure', async () => {
  const response = deferred()
  const form = machineForm(() => response.promise)
  form.openAddForm(); form.form.machine_id = 'new-worker'
  const pending = form.submit()
  assert.equal(form.showForm.value, true)
  assert.equal(form.saving.value, true)
  response.reject(new Error('bad address')); await pending
  assert.equal(form.form.machine_id, 'new-worker')
  assert.equal(form.showForm.value, true)
  assert.equal(form.formError.value, 'bad address')
})

test('machine form closes after write success even if list refresh fails', async () => {
  const state = createConsoleState(api({ getMachines: async () => { throw new Error('refresh offline') } }))
  const form = machineForm(state.handleSaveMachine)
  form.openAddForm(); form.form.machine_id = 'new-worker'
  await form.submit()
  assert.equal(form.showForm.value, false)
  assert.match(state.error.value, /机器已保存/)
})

test('deletion clears selected detail, prevents duplicate writes and keeps a success receipt on refresh failure', async () => {
  const response = deferred(); let writes = 0
  const state = createConsoleState(api({ deleteJob: () => { writes++; return response.promise },
    getJobs: async () => { throw new Error('refresh offline') } }))
  await state.handleSelectJob('A')
  state.jobs.value = [{ job_id: 'A' }]
  state.jobListPage.value = { offset: 0, limit: 20, total: 1 }
  const first = state.handleDeleteJob('A'); const second = state.handleDeleteJob('A')
  assert.equal(writes, 1)
  response.resolve({ deleted: true, resources_preserved: true })
  await Promise.all([first, second])
  assert.equal(state.selectedJobId.value, '')
  assert.equal(state.selectedJob.value, null)
  assert.deepEqual(state.jobs.value, [])
  assert.match(state.message.value, /保留/)
  assert.match(state.error.value, /任务已删除/)
})

test('late deletion cannot clear the new selection and failed deletion keeps the original detail', async () => {
  const response = deferred()
  const state = createConsoleState(api({ deleteJob: () => response.promise }))
  await state.handleSelectJob('A')
  const operation = state.handleDeleteJob('A')
  await state.handleSelectJob('B')
  response.resolve({ deleted: true }); await operation
  assert.equal(state.selectedJob.value.job_id, 'B')
  const rejected = createConsoleState(api({ deleteJob: async () => { throw new Error('请先取消') } }))
  await rejected.handleSelectJob('A'); await rejected.handleDeleteJob('A')
  assert.equal(rejected.selectedJob.value.job_id, 'A')
  assert.match(rejected.error.value, /请先取消/)
})

test('a pre-deletion detail response cannot resurrect the deleted task', async () => {
  const response = deferred(); let slow = false
  const state = createConsoleState(api({ deleteJob: async () => ({ deleted: true }),
    getJob: (id) => slow ? response.promise : Promise.resolve({ job_id: id }) }))
  await state.handleSelectJob('A'); slow = true
  const oldRequest = state.handleJobPage({ offset: 20 })
  await state.handleDeleteJob('A')
  response.resolve({ job_id: 'A' }); await oldRequest
  assert.equal(state.selectedJob.value, null)
  assert.equal(state.jobPageLoading.value, false)
})

test('task sort is sent to the server and survives refresh and selection', async () => {
  const calls = []
  const state = createConsoleState(api({ getJobs: async (options) => {
    calls.push({ ...options })
    return { jobs: [], page: { offset: 0, limit: 20, total: 0 }, running_count: 0 }
  } }))
  await state.handleJobListPage({ sort: 'name_asc', offset: 0 })
  await state.handleSelectJob('A'); await state.refreshDashboard()
  assert.equal(state.jobListOptions.value.sort, 'name_asc')
  assert.deepEqual(calls.map((options) => options.sort), ['name_asc', 'name_asc'])
})

test('delete dialog only emits after explicit confirmation and cancels on selection change', async () => {
  const source = fs.readFileSync(new URL('../src/views/JobsView.vue', import.meta.url), 'utf8')
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import[^\n]*\n/gm, '')
  const props = vue.reactive({ selectedJobId: 'A', selectedJob: { job_id: 'A', name: '任务A', status: 'FAILED' },
    pageOptions: {}, listOptions: {}, listPage: { offset: 0, limit: 20, total: 1 } })
  const events = []
  const env = { ...vue, onBeforeUnmount: () => {}, defineProps: () => props, defineEmits: () => (...args) => events.push(args) }
  const form = new Function('env', `with(env){${script};return {requestDelete, confirmDelete, deleteCandidate}}`)(env)
  form.requestDelete()
  assert.equal(form.deleteCandidate.value.id, 'A')
  assert.equal(events.length, 0)
  form.deleteCandidate.value = null
  form.confirmDelete()
  assert.equal(events.length, 0)
  form.requestDelete(); props.selectedJobId = 'B'; await vue.nextTick()
  form.confirmDelete()
  assert.equal(events.length, 0)
  props.selectedJobId = 'A'; await vue.nextTick()
  form.requestDelete(); form.confirmDelete(); form.confirmDelete()
  assert.deepEqual(events, [['delete', 'A']])
})
