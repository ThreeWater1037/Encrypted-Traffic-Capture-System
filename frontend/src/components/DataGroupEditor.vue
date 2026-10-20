<script setup>
import { onMounted, ref } from 'vue'
import * as api from '../services/api'

const props = defineProps({ groupId: { type: String, default: '' } })
const emit = defineEmits(['close', 'saved'])
const name = ref(''), selected = ref(new Set()), revision = ref(0), ready = ref(false)
const jobs = ref([]), page = ref({ offset: 0, limit: 10, total: 0 }), query = ref('')
const loading = ref(false), saving = ref(false), error = ref('')
let version = 0
async function loadJobs(offset = 0) {
  const current = ++version; loading.value = true
  try {
    const result = await api.getJobs({ query: query.value, offset, limit: 10, sort: 'created_desc' })
    if (current === version) { jobs.value = result.jobs; page.value = result.page }
  } catch (reason) { if (current === version) error.value = reason.message }
  finally { if (current === version) loading.value = false }
}
function toggle(id) {
  const next = new Set(selected.value); next.has(id) ? next.delete(id) : next.add(id); selected.value = next
}
async function save() {
  if (!ready.value || saving.value || !name.value.trim()) return
  saving.value = true; error.value = ''
  try {
    const result = await api.saveDataGroup(props.groupId, { name: name.value.trim(), job_ids: [...selected.value], revision: revision.value })
    emit('saved', { id: result.id, name: name.value.trim() })
  } catch (reason) { error.value = reason.message }
  finally { saving.value = false }
}
onMounted(async () => {
  try {
    if (props.groupId) {
      const result = await api.getDataGroup(props.groupId)
      name.value = result.name; selected.value = new Set(result.job_ids); revision.value = result.revision
    } else revision.value = (await api.getDataGroups({ limit: 1 })).revision
    ready.value = true; await loadJobs()
  } catch (reason) { error.value = reason.message }
})
</script>

<template>
  <div class="modal-backdrop" @keydown.esc="!saving && emit('close')">
    <section class="modal-card data-group-editor" role="dialog" aria-modal="true" aria-labelledby="group-editor-title">
      <div class="data-section-head"><h2 id="group-editor-title">{{ groupId ? '整理总体任务' : '新建总体任务' }}</h2><button :disabled="saving" class="secondary-button" @click="emit('close')">关闭</button></div>
      <label class="data-field">总体任务名称<input v-model="name" maxlength="120" :disabled="!ready || saving" /></label>
      <form class="data-inline" @submit.prevent="loadJobs()"><input v-model="query" placeholder="搜索批次名称或 ID" aria-label="搜索待归组批次" /><button class="secondary-button" :disabled="loading || !ready">查询批次</button></form>
      <p class="data-note">已选 {{ selected.size }} 个批次。保存后，所选批次归入此组；取消选择的原成员转为未归组。</p>
      <p v-if="error" class="load-error" role="alert">{{ error }}</p>
      <p v-if="loading" role="status">正在查询批次…</p>
      <div v-else class="data-candidates">
        <label v-for="job in jobs" :key="job.job_id" class="data-candidate"><input type="checkbox" :checked="selected.has(job.job_id)" :disabled="saving" @change="toggle(job.job_id)" /><span>{{ job.name }}<small>{{ job.job_id }}</small></span></label>
        <p v-if="!jobs.length">暂无匹配批次</p>
      </div>
      <div class="data-pagination"><span>共 {{ page.total }} 个批次 · {{ Math.floor(page.offset / page.limit) + 1 }} / {{ Math.max(1, Math.ceil(page.total / page.limit)) }} 页</span><div class="data-inline"><button class="secondary-button" :disabled="loading || page.offset === 0" @click="loadJobs(page.offset - page.limit)">上一页</button><button class="secondary-button" :disabled="loading || page.offset + page.limit >= page.total" @click="loadJobs(page.offset + page.limit)">下一页</button></div></div>
      <div class="data-inline"><button class="primary-button" :disabled="!ready || saving || !name.trim()" @click="save">{{ saving ? '保存中…' : '保存分组' }}</button><span class="data-note">已有其他归属的所选批次会移入此组。</span></div>
    </section>
  </div>
</template>
