<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Activity,
  FlaskConical,
  History,
  LayoutDashboard,
  RefreshCw,
  Server,
  ShieldCheck,
  Wifi,
  WifiOff,
} from '@lucide/vue'
import WorkspaceView from './views/WorkspaceView.vue'
import JobsView from './views/JobsView.vue'
import MachinesView from './views/MachinesView.vue'
import {
  cancelJob,
  createFileJob,
  createJsonJob,
  deleteMachine,
  getHealth,
  getJob,
  getJobLogs,
  getJobs,
  getMachines,
  probeMachine,
  saveMachine,
} from './services/api'

const activeView = ref('workspace')
const health = ref(null)
const machines = ref([])
const jobs = ref([])
const selectedJob = ref(null)
const selectedLogs = ref([])
const loading = ref(false)
const message = ref('')
const error = ref('')

const navItems = [
  { id: 'workspace', label: '实验工作台', icon: LayoutDashboard },
  { id: 'jobs', label: '任务与结果', icon: History },
  { id: 'machines', label: '机器管理', icon: Server },
]

const runningCount = computed(() =>
  jobs.value.filter((job) => !['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'].includes(job.status)).length,
)

function notify(text) {
  message.value = text
  window.setTimeout(() => { message.value = '' }, 2800)
}

function showError(reason) {
  error.value = reason?.message || String(reason)
  window.setTimeout(() => { error.value = '' }, 5000)
}

async function loadMachines({ probeUnknown = false } = {}) {
  const response = await getMachines()
  machines.value = response.machines
  if (probeUnknown) {
    const unknown = machines.value.filter((machine) => machine.enabled && machine.status === 'UNKNOWN')
    if (unknown.length) {
      await Promise.allSettled(unknown.map((machine) => probeMachine(machine.machine_id)))
      machines.value = (await getMachines()).machines
    }
  }
}

async function loadJobs() {
  jobs.value = (await getJobs()).jobs
  if (selectedJob.value) {
    const latest = jobs.value.find((job) => job.job_id === selectedJob.value.job_id)
    if (latest) selectedJob.value = latest
  }
}

async function loadInitialData() {
  loading.value = true
  try {
    const [healthData] = await Promise.all([
      getHealth(),
      loadMachines({ probeUnknown: true }),
      loadJobs(),
    ])
    health.value = healthData
  } catch (reason) {
    showError(reason)
  } finally {
    loading.value = false
  }
}

async function handleSubmit(submission) {
  loading.value = true
  try {
    const job = submission.kind === 'file'
      ? await createFileJob(submission.form)
      : await createJsonJob(submission.payload)
    await loadJobs()
    selectedJob.value = job
    activeView.value = 'jobs'
    notify(`任务 ${job.job_id} 已提交`)
  } catch (reason) {
    showError(reason)
  } finally {
    loading.value = false
  }
}

async function handleSelectJob(jobId) {
  try {
    selectedJob.value = await getJob(jobId)
    selectedLogs.value = []
  } catch (reason) {
    showError(reason)
  }
}

async function handleCancel(jobId) {
  try {
    selectedJob.value = await cancelJob(jobId)
    await loadJobs()
    notify('取消请求已发送')
  } catch (reason) {
    showError(reason)
  }
}

async function handleLogs(jobId) {
  try {
    selectedLogs.value = (await getJobLogs(jobId)).logs
  } catch (reason) {
    showError(reason)
  }
}

async function handleProbe(machineId) {
  try {
    await probeMachine(machineId)
    await loadMachines()
    notify('机器能力已刷新')
  } catch (reason) {
    await loadMachines()
    showError(reason)
  }
}

async function handleSaveMachine(payload) {
  try {
    await saveMachine(payload)
    await loadMachines()
    notify('机器配置已保存')
  } catch (reason) {
    showError(reason)
  }
}

async function handleDeleteMachine(machineId) {
  try {
    await deleteMachine(machineId)
    await loadMachines()
    notify('机器已删除')
  } catch (reason) {
    showError(reason)
  }
}

let pollTimer
onMounted(() => {
  loadInitialData()
  pollTimer = window.setInterval(async () => {
    if (!runningCount.value && !selectedJob.value) return
    try {
      await loadJobs()
      if (selectedJob.value) selectedJob.value = await getJob(selectedJob.value.job_id)
    } catch {
      // 短暂网络波动交给下一轮轮询恢复，避免连续弹出提示。
    }
  }, 2000)
})
onBeforeUnmount(() => window.clearInterval(pollTimer))
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark"><FlaskConical :size="20" /></span>
        <div><strong>FlowLab</strong><small>流量实验主控</small></div>
      </div>

      <nav>
        <button
          v-for="item in navItems"
          :key="item.id"
          :class="['nav-item', { active: activeView === item.id }]"
          @click="activeView = item.id"
        >
          <component :is="item.icon" :size="17" />
          <span>{{ item.label }}</span>
          <b v-if="item.id === 'jobs' && runningCount">{{ runningCount }}</b>
        </button>
      </nav>

      <div class="sidebar-status">
        <div :class="['status-dot', health ? 'online' : 'offline']"></div>
        <div>
          <strong>{{ health ? '主控运行中' : '主控未连接' }}</strong>
          <small>127.0.0.1:5200</small>
        </div>
      </div>
    </aside>

    <main>
      <header class="topbar">
        <div>
          <span class="eyebrow">CONTROL PLANE</span>
          <h1>{{ navItems.find((item) => item.id === activeView)?.label }}</h1>
        </div>
        <div class="topbar-meta">
          <span><Activity :size="15" /> {{ runningCount }} 个任务运行中</span>
          <span><Server :size="15" /> {{ machines.filter((m) => ['ONLINE', 'BUSY'].includes(m.status)).length }}/{{ machines.length }} 台在线</span>
          <button class="icon-button" title="刷新" @click="loadInitialData"><RefreshCw :size="16" /></button>
        </div>
      </header>

      <section class="page-content">
        <WorkspaceView
          v-show="activeView === 'workspace'"
          :machines="machines"
          :loading="loading"
          @submit="handleSubmit"
          @probe="handleProbe"
        />
        <JobsView
          v-if="activeView === 'jobs'"
          :jobs="jobs"
          :selected-job="selectedJob"
          :logs="selectedLogs"
          @select="handleSelectJob"
          @cancel="handleCancel"
          @logs="handleLogs"
          @refresh="loadJobs"
        />
        <MachinesView
          v-if="activeView === 'machines'"
          :machines="machines"
          @delete="handleDeleteMachine"
          @probe="handleProbe"
          @save="handleSaveMachine"
        />
      </section>
    </main>

    <transition name="toast">
      <div v-if="message" class="toast success"><ShieldCheck :size="17" />{{ message }}</div>
    </transition>
    <transition name="toast">
      <div v-if="error" class="toast error"><WifiOff :size="17" />{{ error }}</div>
    </transition>
  </div>
</template>
