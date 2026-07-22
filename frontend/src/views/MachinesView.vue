<script setup>
import { reactive, ref } from 'vue'
import { Pencil, Plus, RefreshCw, Server, ShieldCheck, Trash2, X } from '@lucide/vue'
import StatusPill from '../components/StatusPill.vue'

defineProps({ machines: { type: Array, default: () => [] } })
const emit = defineEmits(['delete', 'probe', 'save'])
const showForm = ref(false)
const editingMachineId = ref(null)
const form = reactive({ machine_id: '', name: '', base_url: 'http://127.0.0.1:5100', token: '', enabled: true })

function resetForm() {
  editingMachineId.value = null
  Object.assign(form, { machine_id: '', name: '', base_url: 'http://127.0.0.1:5100', token: '', enabled: true })
}

function openAddForm() {
  resetForm()
  showForm.value = true
}

function openEditForm(machine) {
  editingMachineId.value = machine.machine_id
  Object.assign(form, {
    machine_id: machine.machine_id,
    name: machine.name,
    base_url: machine.base_url,
    token: '',
    enabled: machine.enabled,
  })
  showForm.value = true
}

function closeForm() {
  showForm.value = false
  resetForm()
}

function confirmDelete(machine) {
  const confirmed = window.confirm(`确定删除机器“${machine.name}”（${machine.machine_id}）吗？此操作不可撤销。`)
  if (confirmed) emit('delete', machine.machine_id)
}

function submit() {
  const payload = {
    machine_id: form.machine_id,
    name: form.name || form.machine_id,
    base_url: form.base_url,
    enabled: form.enabled,
  }
  // 编辑时 Token 留空表示保留主控中已有的密钥，避免前端读取或回显敏感信息。
  if (form.token) payload.token = form.token
  emit('save', payload)
  closeForm()
}
</script>

<template>
  <div class="machines-page">
    <div class="page-intro">
      <div><span class="eyebrow">WORKER REGISTRY</span><h2>子机器与执行能力</h2><p>主控保存 Worker 地址和 Token；Token 不会发送给前端。</p></div>
      <button class="primary-button" @click="openAddForm"><Plus :size="16" />添加机器</button>
    </div>

    <div class="machine-grid">
      <article v-for="machine in machines" :key="machine.machine_id" class="panel machine-card">
        <header><span class="machine-icon"><Server :size="20" /></span><div><strong>{{ machine.name }}</strong><code>{{ machine.machine_id }}</code></div><StatusPill :status="machine.status" /></header>
        <dl>
          <div><dt>Worker 地址</dt><dd>{{ machine.base_url }}</dd></div>
          <div><dt>操作系统</dt><dd>{{ machine.capabilities?.os || '等待探测' }} {{ machine.capabilities?.architecture || '' }}</dd></div>
          <div><dt>浏览器</dt><dd class="browser-tags"><span v-for="browser in machine.capabilities?.browsers || []" :key="browser.name">{{ browser.name }} <ShieldCheck v-if="browser.no_cache_verified" :size="11" /></span><em v-if="!machine.capabilities?.browsers?.length">未知</em></dd></div>
          <div><dt>采集能力</dt><dd>{{ machine.capabilities?.capture?.pcap ? 'PCAP 可用' : '尚未确认 PCAP' }}</dd></div>
          <div><dt>最后连接</dt><dd>{{ machine.last_seen_at ? new Date(machine.last_seen_at).toLocaleString() : '从未连接' }}</dd></div>
        </dl>
        <p v-if="machine.last_error" class="machine-error">{{ machine.last_error }}</p>
        <footer>
          <span>{{ machine.enabled ? '已启用' : '已停用' }} · Token {{ machine.token_configured ? '已配置' : '未配置' }}</span>
          <div class="machine-actions">
            <button class="secondary-button" @click="openEditForm(machine)"><Pencil :size="14" />编辑</button>
            <button class="secondary-button" @click="emit('probe', machine.machine_id)"><RefreshCw :size="14" />重新探测</button>
            <button class="danger-button" @click="confirmDelete(machine)"><Trash2 :size="14" />删除</button>
          </div>
        </footer>
      </article>
    </div>

    <div v-if="showForm" class="modal-backdrop" @click.self="closeForm">
      <form class="modal-card" @submit.prevent="submit">
        <header>
          <div>
            <h3>{{ editingMachineId ? '编辑 Worker' : '添加 Worker' }}</h3>
            <p>{{ editingMachineId ? '可修改名称、地址、启用状态；Token 留空时保持不变。' : '本机之外的机器只需替换 IP 与 Token。' }}</p>
          </div>
          <button type="button" class="icon-button" @click="closeForm"><X :size="17" /></button>
        </header>
        <label class="field"><span>机器 ID</span><input v-model="form.machine_id" required :disabled="Boolean(editingMachineId)" placeholder="win-lab-01" /></label>
        <label class="field"><span>显示名称</span><input v-model="form.name" placeholder="Windows 实验机" /></label>
        <label class="field"><span>Worker 地址</span><input v-model="form.base_url" required placeholder="http://192.168.1.20:5100" /></label>
        <label class="field">
          <span>Worker Token</span>
          <input v-model="form.token" :required="!editingMachineId" type="password" autocomplete="new-password" :placeholder="editingMachineId ? '留空表示保持原 Token' : '请输入 Worker Token'" />
        </label>
        <label class="check-line machine-enabled"><input v-model="form.enabled" type="checkbox" />启用这台机器</label>
        <button class="primary-button" type="submit">{{ editingMachineId ? '保存修改' : '保存机器' }}</button>
      </form>
    </div>
  </div>
</template>
