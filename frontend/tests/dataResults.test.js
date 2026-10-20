import test from 'node:test'
import assert from 'node:assert/strict'
import { createDataResults, fileLabel } from '../src/services/dataResults.js'

const summary = { stats: { records: 1, urls: 1, batches: 1 }, machines: [] }
const result = (name, offset = 0) => ({ rows: [{ execution_id: name }], page: { offset, limit: 20, total: 80 } })
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const api = changes => ({ getDataOverview: async () => summary, getDataResults: async () => result('default'),
  getDataResult: async q => ({ execution_id: q.execution_id }), ...changes })

test('late result and detail responses never replace newer filters', async () => {
  const old = deferred(), detail = deferred()
  const state = createDataResults(api({ getDataResults: q => q.query === 'old' ? old.promise : Promise.resolve(result(q.query)),
    getDataResult: q => q.execution_id === 'old' ? detail.promise : Promise.resolve({ execution_id: 'new' }) }))
  const pending = state.changeOptions({ query: 'old' })
  await state.changeOptions({ query: 'new' }); old.resolve(result('old')); await pending
  assert.equal(state.rows.value[0].execution_id, 'new')
  const pendingDetail = state.openDetail({ execution_id: 'old' })
  await state.openDetail({ execution_id: 'new' }); detail.resolve({ execution_id: 'old' }); await pendingDetail
  assert.equal(state.detail.value.execution_id, 'new')
})

test('returning to a group restores filters and page without refetching cached results', async () => {
  let calls = 0
  const state = createDataResults(api({ getDataResults: async q => { calls++; return result(q.group, q.offset) } }))
  await state.selectGroup('a', 'A'); await state.changeOptions({ browser: 'firefox', machine: 'm', offset: 20 })
  await state.selectGroup('b', 'B'); const before = calls; await state.selectGroup('a', 'A')
  assert.deepEqual(state.options.value, { query: '', browser: 'firefox', machine: 'm', offset: 20, limit: 20 })
  assert.equal(calls, before); assert.equal(state.rows.value[0].execution_id, 'a')
  await state.refresh(); assert.equal(calls, before + 1)
})

test('failed filters hide previous rows and retry recovers; details load only on click', async () => {
  let fail = false, reads = 0
  const state = createDataResults(api({ getDataResults: async () => { if (fail) throw Error('offline'); return result('fresh') },
    getDataResult: async () => { reads++; return { files: [] } } }))
  await state.load(); assert.equal(reads, 0)
  fail = true; await state.changeOptions({ query: 'other' })
  assert.equal(state.error.value, 'offline'); assert.equal(state.rows.value.length, 0)
  fail = false; await state.load(true); assert.equal(state.error.value, '')
  await state.openDetail(state.rows.value[0]); assert.equal(reads, 1)
  state.closeDetail(); await state.openDetail(state.rows.value[0]); assert.equal(reads, 1)
})

test('missing or invalid files cannot be labeled complete', () => {
  assert.equal(fileLabel({ pcap: 'retired', tls: 'present' }), '提取完成 · PCAP 已删除')
  assert.equal(fileLabel({ pcap: 'present', tls: 'present' }), 'PCAP + TLS')
  assert.equal(fileLabel({ pcap: 'present', tls: 'missing' }), '缺少 TLS 密钥')
  assert.equal(fileLabel({ pcap: 'invalid', tls: 'present' }), '文件检查未通过')
})
