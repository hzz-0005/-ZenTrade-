<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  open: Boolean,
  title: String,
  day: Object,
})
const emit = defineEmits(['close'])
const drawerEl = ref(null)
let previousFocus = null
let previousOverflow = ''

function releasePage() {
  document.getElementById('app')?.removeAttribute('inert')
  document.body.style.overflow = previousOverflow
  if (previousFocus instanceof HTMLElement) previousFocus.focus()
  previousFocus = null
}

function onKeydown(event) {
  if (!props.open) return
  if (event.key === 'Escape') {
    emit('close')
    return
  }
  if (event.key !== 'Tab' || !drawerEl.value) return
  const focusable = [...drawerEl.value.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(() => props.open, async (open) => {
  if (open) {
    previousFocus = document.activeElement
    previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.getElementById('app')?.setAttribute('inert', '')
    await nextTick()
    drawerEl.value?.querySelector('button:not([disabled])')?.focus()
  } else {
    releasePage()
  }
})
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  releasePage()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="overlay" aria-hidden="true" @click="emit('close')" />
    <aside ref="drawerEl" class="drawer" :class="{ open }" role="dialog" aria-modal="true"
           aria-labelledby="decision-drawer-title" :aria-hidden="!open">
      <div class="drawer-head">
        <div>
          <p class="panel-kicker">Decision audit</p>
          <h2 id="decision-drawer-title">{{ day ? `${day.decision_date || day.date} 决策详情` : title }}</h2>
        </div>
        <button class="icon-btn drawer-close" aria-label="关闭决策详情" @click="emit('close')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
      </div>
      <slot />
    </aside>
  </Teleport>
</template>
