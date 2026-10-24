<script setup lang="ts">
import { computed, ref, watch } from "vue"
import { loadFace } from "./api"

const COLORS = ["#e17076", "#faa774", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae"]

const props = defineProps<{
  name?: string
  peer?: number | null
  mark?: "" | "on" | "off"
  size?: "sm" | "md" | "lg"
}>()

const photo = ref("")

watch(() => props.peer, async (peer) => {
  photo.value = ""
  const url = await loadFace(peer)
  if (Number(props.peer) === Number(peer) && url) photo.value = url
}, { immediate: true })

const letter = computed(() => {
  const text = String(props.name || "").trim().replace(/^@/, "")
  const ch = Array.from(text)[0]
  if (!ch) return "?"
  return ch.toLocaleUpperCase("en-US")
})

const color = computed(() => {
  let n = Number(props.peer)
  if (!Number.isFinite(n) || n === 0) {
    n = 0
    for (const ch of String(props.name || "")) n = (n * 33 + ch.charCodeAt(0)) >>> 0
  }
  return COLORS[Math.abs(Math.trunc(n)) % COLORS.length]
})
</script>

<template>
  <span class="face" :class="[size || 'md', mark]" aria-hidden="true">
    <span class="face-pic" :style="photo ? undefined : { background: color }">
      <img v-if="photo" :src="photo" alt="" />
      <template v-else>{{ letter }}</template>
    </span>
    <i v-if="mark"></i>
  </span>
</template>
