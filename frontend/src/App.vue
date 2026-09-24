<script setup>
import { onBeforeUnmount, onMounted } from 'vue'
import { Activity, FlaskConical, History, LayoutDashboard, RefreshCw, Server, ShieldCheck, WifiOff } from '@lucide/vue'
import WorkspaceView from './views/WorkspaceView.vue'
import JobsView from './views/JobsView.vue'
import MachinesView from './views/MachinesView.vue'
import * as api from './services/api'
import { createConsoleState } from './services/consoleState'

const {
  activeView, health, connectionError, lastUpdated, machines, machinesLoaded, jobs,
  jobListOptions, jobListPage, jobListLoading, jobListError, runningCount,
  selectedJobId, selectedJob, jobPageOptions, jobPageLoading, jobPageError,
  selectedLogs, logsLoading, loading, refreshing, message, error, actionBusy,
  loadInitialData, refreshDashboard, handleSubmit, handleSelectJob, handleJobPage,
  handleJobListPage, handleCancel, handleResume, handleRestart, handleDeleteJob, handleLogs,
  handleProbe, handleSaveMachine, handleDeleteMachine,
} = createConsoleState(api)
const navItems = [
  { id: 'workspace', label: '实验工作台', icon: LayoutDashboard },
  { id: 'jobs', label: '任务与结果', icon: History },
  { id: 'machines', label: '机器管理', icon: Server },
]
let pollTimer
onMounted(() => {
  loadInitialData()
  pollTimer = window.setInterval(() => refreshDashboard(), 5 * 60 * 1000)
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
          <small class="api-address" :title="api.API_BASE">{{ api.API_BASE }}</small>
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
          <span>每 5 分钟自动刷新</span>
          <button class="secondary-button" :disabled="loading || refreshing" :aria-busy="refreshing" @click="refreshDashboard(true)"><RefreshCw :size="16" />{{ refreshing ? '刷新中…' : '立即刷新' }}</button>
        </div>
      </header>

      <section class="page-content">
        <p v-if="connectionError" class="load-error" role="alert">主控未连接，当前数据可能已过期：{{ connectionError }}</p>
        <p v-if="lastUpdated" class="update-time">主控最近连接成功：{{ lastUpdated }}</p>
        <WorkspaceView
          v-show="activeView === 'workspace'"
          :machines="machines"
          :machines-loaded="machinesLoaded"
          :loading="loading"
          @submit="handleSubmit"
          @probe="handleProbe"
        />
        <JobsView
          v-show="activeView === 'jobs'"
          :jobs="jobs"
          :selected-job-id="selectedJobId"
          :list-options="jobListOptions"
          :list-page="jobListPage"
          :list-loading="jobListLoading"
          :list-error="jobListError"
          :page-error="jobPageError"
          :action-busy="actionBusy"
          @list-page="handleJobListPage"
          :selected-job="selectedJob"
          :logs="selectedLogs"
          :logs-loading="logsLoading"
          :page-loading="jobPageLoading"
          :refreshing="loading || refreshing"
          :page-options="jobPageOptions"
          @page="handleJobPage"
          @select="handleSelectJob"
          @cancel="handleCancel"
          @resume="handleResume"
          @restart="handleRestart"
          @delete="handleDeleteJob"
          @logs="handleLogs"
          @refresh="refreshDashboard(true)"
        />
        <MachinesView
          v-show="activeView === 'machines'"
          :machines="machines"
          @delete="handleDeleteMachine"
          @probe="handleProbe"
          :save-machine="handleSaveMachine"
        />
      </section>
    </main>

    <transition name="toast">
      <div v-if="message" class="toast success"><ShieldCheck :size="17" />{{ message }}<button aria-label="关闭提示" @click="message = ''">×</button></div>
    </transition>
    <transition name="toast">
      <div v-if="error" class="toast error"><WifiOff :size="17" />{{ error }}<button aria-label="关闭错误提示" @click="error = ''">×</button></div>
    </transition>
  </div>
</template>
