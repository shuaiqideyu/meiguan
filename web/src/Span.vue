<script setup lang="ts">
const props = defineProps<{ label: string; low: string; high: string; min?: number; max?: number }>()
const emit = defineEmits<{ "update:low": [string]; "update:high": [string] }>()

function digits(value: string) {
  return value.replace(/\D/g, "").slice(0, 6)
}

function bound(value: string) {
  const text = digits(value)
  if (props.min == null || props.max == null) return text
  const n = Number(text)
  if (!Number.isInteger(n) || n < props.min) return String(props.min)
  if (n > props.max) return String(props.max)
  return String(n)
}

function leaveLow() {
  if (props.min == null || props.max == null) return
  const next = bound(props.low)
  const stop = bound(props.high)
  emit("update:low", next)
  if (Number(stop) < Number(next)) emit("update:high", next)
}

function leaveHigh() {
  if (props.min == null || props.max == null) return
  const start = bound(props.low)
  let next = bound(props.high)
  if (Number(next) < Number(start)) next = start
  emit("update:low", start)
  emit("update:high", next)
}
</script>

<template>
  <div class="span-row">
    <span class="span-name">{{ label }}</span>
    <span class="span-boxes">
      <input
        :value="low"
        inputmode="numeric"
        autocomplete="off"
        aria-label="起"
        @input="emit('update:low', digits(($event.target as HTMLInputElement).value))"
        @blur="leaveLow"
      />
      <i>-</i>
      <input
        :value="high"
        inputmode="numeric"
        autocomplete="off"
        aria-label="止"
        @input="emit('update:high', digits(($event.target as HTMLInputElement).value))"
        @blur="leaveHigh"
      />
      <span class="span-unit">秒</span>
    </span>
  </div>
</template>

<style scoped>
.span-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 48px;
  padding: 8px 16px;
  background: #fff;
}

.span-name {
  flex: none;
  font-size: 16px;
  line-height: 1.2;
}

.span-boxes {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.span-unit {
  color: var(--td-text-color-secondary, rgba(0, 0, 0, 0.6));
  font-size: 14px;
}

.span-boxes input {
  width: 76px;
  height: 32px;
  margin: 0;
  padding: 0 8px;
  box-sizing: border-box;
  border: 1px solid var(--td-gray-color-3, #e7e7e7);
  border-radius: 6px;
  background: #fff;
  color: inherit;
  font-size: 16px;
  text-align: center;
}

.span-boxes i {
  color: var(--td-text-color-secondary, rgba(0, 0, 0, 0.6));
  font-style: normal;
}
</style>
