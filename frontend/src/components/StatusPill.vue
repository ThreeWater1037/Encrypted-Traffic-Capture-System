<script setup>
import { statusLabel } from '../services/status'
defineProps({ status: { type: String, default: 'UNKNOWN' } })
</script>

<template>
  <span :class="['status-pill', `status-${(status || 'UNKNOWN').toLowerCase()}`]">
    <i></i>{{ statusLabel(status) }}
  </span>
</template>
