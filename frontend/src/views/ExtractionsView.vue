<script setup>
import { onBeforeUnmount, watch } from 'vue'
import StatusPill from '../components/StatusPill.vue'
import * as api from '../services/api'
import { createExtractions, terminalExtraction } from '../services/extractions'
const props = defineProps({ active: Boolean, machines: { type: Array, default: () => [] } })
const { machine, query, candidates, jobs, detail, page, queuePage, loading, detailLoading, busy, error, queueError, message,
  keepPcap, retentionMode, keepPcapCount, selectMachine, load, openDetail, submit, cancel, refresh } = createExtractions(api)
const retentionLabel = job => !job.keep_pcap ? '不保留（校验成功后清理）' : job.keep_pcap_count == null ? '全部保留' : `保留前 ${job.keep_pcap_count} 个`
let timer
const stageLabel = stage => ({ preparing: '准备中', 'extract_features.py': '流特征提取', 'classify_packets.py': '解密流分类',
  'infer_packets.py': '流推断', 'extract_header_seqs.py': '流序列提取', 'verify_sequences.py': '流序列校验',
  'extract_tls.py': 'TLS 特征提取' }[stage] || '流序列与 TLS 顺序提取')
watch(() => [props.active, props.machines.length], () => {
  clearInterval(timer)
  if (!props.active) return
  if (!machine.value) {
    const first = props.machines.find(m => m.enabled)
    if (first) selectMachine(first.machine_id)
  } else refresh()
  timer = setInterval(() => { if (!loading.value && !detailLoading.value && !busy.value) refresh() }, 15000)
}, { immediate: true })
onBeforeUnmount(() => clearInterval(timer))
function search() { page.value.offset = 0; load() }
function movePage(target, delta) { target.offset += delta * target.limit; load() }
</script>

<template>
  <div class="extraction-page">
    <section class="panel extraction-intro">
      <div><h2>完成任务提取</h2><p>流序列 → TLS 特征 → 校验 → 按所选策略保留或清理 PCAP。TLS 密钥、提取结果和失败项 PCAP 始终保留。</p></div>
      <p>每台服务器依次处理一个 PCAP，与抓包互斥。有抓包任务排队时，在当前 PCAP 处理完后优先抓包。原有后台提取功能保持不变。</p>
      <div class="extraction-toolbar">
        <label>服务器 <select :value="machine" :disabled="busy" @change="selectMachine($event.target.value)"><option value="" disabled>选择服务器</option><option v-for="m in machines.filter(m => m.enabled)" :key="m.machine_id" :value="m.machine_id">{{ m.name }} · {{ m.machine_id }}</option></select></label>
        <button class="secondary-button" :disabled="loading || busy || !machine" @click="refresh">{{ loading ? '刷新中…' : '刷新' }}</button><span>本页每 15 秒刷新</span>
      </div>
      <p v-if="error" class="load-error" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    </section>

    <section class="panel">
      <div class="extraction-toolbar"><h2>选择已完成任务</h2><form @submit.prevent="search"><input v-model="query" aria-label="搜索已完成任务" placeholder="任务名称或 ID" /><button class="secondary-button" :disabled="loading || !machine">搜索</button></form></div>
      <p>自动处理原任务中 Chrome、Edge、Firefox 的采集结果。部分成功任务中没有完整检查点的单元会保留并报告失败。</p>
      <div class="extraction-toolbar retention-options">
        <label><input v-model="keepPcap" type="checkbox" :disabled="busy" /> 保留原始 PCAP</label>
        <label v-if="keepPcap">保留范围 <select v-model="retentionMode" :disabled="busy"><option value="first">前 N 个</option><option value="all">全部</option></select></label>
        <label v-if="keepPcap && retentionMode === 'first'">保留数量 <input v-model.number="keepPcapCount" type="number" min="1" step="1" :disabled="busy" aria-label="保留 PCAP 数量" /></label>
      </div>
      <p>新提交任务使用以上设置。不勾选则在校验成功后清理原始 PCAP；前 N 个按原任务 URL 顺序、同一 URL 内 Chrome → Edge → Firefox 顺序计数，每个浏览器单独计一个。缺失或失败单元仍占顺序位置，失败项额外保留；重试沿用首次设置。</p>
      <div class="extraction-table-wrap"><table class="extraction-table"><thead><tr><th>任务</th><th>采集状态</th><th>浏览器</th><th>单元数</th><th>操作</th></tr></thead>
        <tbody><tr v-for="job in candidates" :key="job.job_id"><td><strong>{{ job.name }}</strong><small>{{ job.worker_task_id }}</small></td><td><StatusPill :status="job.status" /></td><td>{{ job.browsers.join(' / ') }}</td><td>{{ job.units }}</td><td><button class="primary-button" :disabled="busy || loading || !!queueError" @click="submit(job.job_id)">开始提取</button></td></tr></tbody></table></div>
      <p v-if="!candidates.length && !loading" class="empty-state">{{ machine ? '没有符合条件的已完成任务' : '请选择服务器' }}</p>
      <div class="extraction-pagination"><span>共 {{ page.total }} 个任务</span><button :disabled="loading || !page.offset" @click="movePage(page, -1)">上一页</button><span>{{ Math.floor(page.offset / page.limit) + 1 }} / {{ Math.max(1, Math.ceil(page.total / page.limit)) }}</span><button :disabled="loading || page.offset + page.limit >= page.total" @click="movePage(page, 1)">下一页</button></div>
    </section>

    <section class="panel">
      <h2>提取队列与记录</h2><p v-if="queueError" class="load-error" role="alert">{{ queueError }}</p>
      <div class="extraction-table-wrap"><table class="extraction-table"><thead><tr><th>原任务</th><th>提取状态</th><th>进度</th><th>成功 / 失败 / 已保留 / 已清理</th><th>操作</th></tr></thead><tbody>
        <tr v-for="job in jobs" :key="job.task_id"><td><strong>{{ job.job?.name || job.source_task_id }}</strong><small>{{ job.source_task_id }}</small><small>{{ retentionLabel(job) }}</small></td><td><StatusPill :status="job.status" /><small v-if="job.waiting_for_capture">等待调度，抓包优先</small></td><td>{{ job.progress.cursor || 0 }} / {{ job.progress.total || '待扫描' }}</td><td>{{ job.progress.succeeded || 0 }} / {{ job.progress.failed || 0 }} / {{ job.progress.retained || 0 }} / {{ job.progress.deleted || 0 }}</td><td class="extraction-actions"><button class="secondary-button" @click="openDetail(job.task_id)">详情</button><button v-if="!terminalExtraction(job.status)" :disabled="busy" @click="cancel(job.task_id)">取消提取</button><button v-else-if="job.status !== 'SUCCEEDED' && job.job" :disabled="busy" @click="submit(job.job.job_id, job)">重试未成功项</button></td></tr>
      </tbody></table></div><p v-if="!jobs.length && !queueError" class="empty-state">暂无独立提取记录</p>
      <div class="extraction-pagination"><span>共 {{ queuePage.total }} 条</span><button :disabled="loading || !queuePage.offset" @click="movePage(queuePage, -1)">上一页</button><button :disabled="loading || queuePage.offset + queuePage.limit >= queuePage.total" @click="movePage(queuePage, 1)">下一页</button></div>
    </section>

    <section v-if="detail" class="panel extraction-detail">
      <div class="extraction-toolbar"><h2>{{ detail.job?.name || detail.source_task_id }} · 提取详情</h2><StatusPill :status="detail.status" /></div>
      <p>PCAP 策略：{{ retentionLabel(detail) }}。已保留 {{ detail.progress.retained || 0 }} 个，已清理 {{ detail.progress.deleted || 0 }} 个（均为校验成功项；失败项另行保留）。</p>
      <p v-if="detail.progress.current">当前：{{ detail.progress.current.name }} · {{ detail.progress.current.browser }} · {{ stageLabel(detail.progress.current_stage) }}</p>
      <p v-if="detail.error" class="load-error">{{ detail.error }}</p>
      <p v-if="detail.sync_error" class="load-error">{{ detail.sync_error }}</p>
      <dl><dt>服务器结果根目录</dt><dd>{{ detail.output_root }}</dd><dt>每个 URL 的结果位置</dt><dd>{{ detail.result_layout }}</dd><dt>校验凭据</dt><dd>&lt;URL目录&gt;/extraction_&lt;browser&gt;.json</dd></dl>
      <p>流序列位于结果目录的 sequences/decrypted 和 sequences/inferred；TLS 特征位于 tls。双向、上行、下行 TSV 均保留。</p>
      <div v-if="detail.progress.errors?.length"><h3>失败项（最近 100 条，完整记录见提取日志）</h3><ul><li v-for="(row, i) in detail.progress.errors" :key="i">{{ row.item_id }} / {{ row.browser }}：{{ row.error }}</li></ul></div>
    </section>
  </div>
</template>

<style scoped>
.extraction-page { display: grid; gap: 20px; }
.extraction-page .panel { padding: 22px; min-width: 0; }
.extraction-page h2 { margin: 0 0 12px; font-size: 18px; }
.extraction-page p { color: var(--muted, #68758a); line-height: 1.7; }
.extraction-toolbar { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.extraction-toolbar form { display: flex; gap: 8px; margin-left: auto; }
.extraction-toolbar select { margin-left: 8px; max-width: 100%; }
.retention-options { margin-top: 16px; }
.retention-options input[type="number"] { width: 110px; }
.extraction-toolbar input, .extraction-toolbar select { padding: 9px 12px; border: 1px solid #dce3ed; border-radius: 8px; font: inherit; background: #fff; color: inherit; }
.extraction-table-wrap { overflow-x: auto; }
.extraction-table { width: 100%; border-collapse: collapse; text-align: left; }
.extraction-table th, .extraction-table td { padding: 14px 10px; border-bottom: 1px solid #e6eaf0; }
.extraction-table th { color: #68758a; font-weight: 500; }
.extraction-table small { display: block; color: #68758a; margin-top: 6px; overflow-wrap: anywhere; }
.extraction-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.extraction-pagination { display: flex; gap: 12px; align-items: center; justify-content: flex-end; margin-top: 16px; }
.extraction-detail dd { margin: 6px 0 18px; overflow-wrap: anywhere; font-family: monospace; }
.extraction-detail li { overflow-wrap: anywhere; line-height: 1.8; }
.extraction-page .load-error { color: #b42318; }
</style>
