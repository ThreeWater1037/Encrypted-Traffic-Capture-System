import test from 'node:test'
import assert from 'node:assert/strict'
import { createExtractions } from '../src/services/extractions.js'
const page = { offset: 0, limit: 20, total: 1 }
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const base = overrides => ({
  getExtractionCandidates: async q => ({ jobs: [{ job_id: q.machine_id }], page }),
  getExtractions: async machine => ({ jobs: [{ task_id: machine }], page }),
  getExtraction: async (machine, id) => ({ task_id: id, source_task_id: machine }),
  submitExtraction: async (machine, id) => ({ task_id: id, status: 'QUEUED' }),
  cancelExtraction: async () => ({}), ...overrides,
})
test('switching server discards late list and detail responses', async () => {
  const old = deferred(), oldDetail = deferred()
  const state = createExtractions(base({
    getExtractionCandidates: q => q.machine_id === 'a' ? old.promise : Promise.resolve({ jobs: [{ job_id: 'b' }], page }),
    getExtraction: () => oldDetail.promise,
  }))
  const pending = state.selectMachine('a'), detail = state.openDetail('a-job')
  await state.selectMachine('b')
  old.resolve({ jobs: [{ job_id: 'a' }], page }); oldDetail.resolve({ task_id: 'a-job' })
  await Promise.all([pending, detail])
  assert.equal(state.candidates.value[0].job_id, 'b'); assert.equal(state.detail.value, null)
})
test('old Worker reports unsupported separately without hiding completed candidates', async () => {
  const state = createExtractions(base({ getExtractions: async () => { throw Error('Worker 尚未升级') } }))
  await state.selectMachine('a')
  assert.equal(state.candidates.value.length, 1); assert.equal(state.queueError.value, 'Worker 尚未升级')
  assert.equal(state.jobs.value.length, 0)
})
test('submissions are not duplicated while pending and failures are visible', async () => {
  const send = deferred(); let calls = 0
  const state = createExtractions(base({ submitExtraction: () => { calls++; return send.promise } }))
  await state.selectMachine('a')
  const first = state.submit('job'); await state.submit('job')
  assert.equal(calls, 1); send.resolve({ task_id: 'extraction', status: 'QUEUED' }); await first
  assert.equal(state.busy.value, false); assert.equal(state.detail.value.task_id, 'extraction')
})
test('server-side paging and filter are sent to API without resetting queue page', async () => {
  let requested
  const state = createExtractions(base({ getExtractionCandidates: async q => { requested = q; return { jobs: [], page } } }))
  state.machine.value = 'm'; state.query.value = '哈工大'; state.page.value.offset = 20
  await state.load()
  assert.deepEqual(requested, { machine_id: 'm', query: '哈工大', offset: 20, limit: 20 })
})

test('submissions send none, first N, and all retention policies', async () => {
  const requests = []
  const state = createExtractions(base({ submitExtraction: async (machine, id, options) => {
    requests.push(options); return { task_id: id, status: 'QUEUED' }
  } }))
  await state.selectMachine('a')
  await state.submit('default')
  state.keepPcap.value = true; state.keepPcapCount.value = 5
  await state.submit('first-five')
  state.retentionMode.value = 'all'
  await state.submit('all')
  assert.deepEqual(requests, [
    { keep_pcap: false, keep_pcap_count: null },
    { keep_pcap: true, keep_pcap_count: 5 },
    { keep_pcap: true, keep_pcap_count: null },
  ])
})

test('invalid counts never reach API and retry uses saved policy instead of current controls', async () => {
  const requests = []
  const state = createExtractions(base({ submitExtraction: async (machine, id, options) => {
    requests.push(options); return { task_id: id, status: 'QUEUED' }
  } }))
  await state.selectMachine('a'); state.keepPcap.value = true
  for (const count of ['', 0, -1, 1.5, '3', Infinity]) {
    state.keepPcapCount.value = count; await state.submit('invalid')
    assert.match(state.error.value, /正整数/)
  }
  assert.equal(requests.length, 0)
  await state.submit('retry', { keep_pcap: true, keep_pcap_count: 2 })
  await state.submit('legacy-retry', {})
  assert.deepEqual(requests, [{ keep_pcap: true, keep_pcap_count: 2 }, { keep_pcap: false, keep_pcap_count: null }])
})
