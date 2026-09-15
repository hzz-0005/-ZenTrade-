<script setup>
defineProps({
  status: String,
  action: String,
})
import { STATUS, ACTION } from '../api'
</script>

<template>
  <span v-if="status && STATUS[status]" class="badge" :class="STATUS[status].badge">{{ STATUS[status].text }}</span>
  <span v-else-if="action && ACTION[action]" class="badge" :class="ACTION[action].badge">{{ ACTION[action].text }}</span>
</template>
