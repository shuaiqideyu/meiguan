<script setup lang="ts">
import { onUnmounted, ref } from "vue"
import { useRouter } from "vue-router"
import { clockText, hasTelegram } from "../api"
import { setMain } from "../chrome"
import { home, loadHome, patch } from "../state"
import { runFlow } from "../flow"

const router = useRouter()
const openAt = ref("00:00")
const closeAt = ref("00:00")
const error = ref("")
const busy = ref(false)

if (!home.loaded) loadHome().then(fill)
else fill()

function fill() {
  openAt.value = home.policy?.open_at || "00:00"
  closeAt.value = home.policy?.close_at || "00:00"
}

async function save() {
  const open_at = clockText(openAt.value)
  const close_at = clockText(closeAt.value)
  if (!open_at || !close_at) {
    error.value = "时间写成 10:00 这样"
    return
  }
  busy.value = true
  error.value = ""
  setMain("保存", save, true)
  try {
    await runFlow("正在保存", "已保存", () => patch({ open_at, close_at }))
    router.back()
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
    setMain("保存", save, false)
  }
}

if (hasTelegram()) setMain("保存", save)
onUnmounted(() => setMain(null))
</script>

<template>
  <main class="page">
    <t-navbar :fixed="false" title="运作时间" />
    <t-notice-bar v-if="error" theme="error" :content="error" />
    <t-cell-group>
      <t-input v-model="openAt" type="time" label="开始" placeholder="10:00" align="right" />
      <t-input v-model="closeAt" type="time" label="结束" placeholder="02:00" align="right" />
    </t-cell-group>
    <p class="hint">北京时间。开始和结束相同就是全天，可以跨过零点，例如 10:00 到 02:00。这段以外不抢。</p>
    <div v-if="!hasTelegram()" class="bar-gap">
      <t-button theme="primary" block :loading="busy" @click="save">保存</t-button>
    </div>
  </main>
</template>
