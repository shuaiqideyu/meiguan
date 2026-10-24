<script setup lang="ts">
import { onUnmounted, ref } from "vue"
import { useRoute, useRouter } from "vue-router"
import { coinLabel, hasTelegram } from "../api"
import { setMain } from "../chrome"
import { home, loadHome, patch } from "../state"
import { runFlow } from "../flow"

const route = useRoute()
const router = useRouter()
const code = String(route.params.code)
const count = ref("")
const total = ref("")
const each = ref("")
const error = ref("")
const busy = ref(false)

if (!home.loaded) loadHome().then(fill)
else fill()

function fill() {
  count.value = home.policy?.min_counts?.[code] || ""
  total.value = home.policy?.min_totals?.[code] || ""
  each.value = home.policy?.min_eaches?.[code] || ""
}

async function save() {
  busy.value = true
  error.value = ""
  setMain("保存", save, true)
  try {
    await runFlow("正在保存", "已保存", () => patch({
      min_counts: { ...(home.policy?.min_counts || {}), [code]: count.value.trim() },
      min_totals: { ...(home.policy?.min_totals || {}), [code]: total.value.trim() },
      min_eaches: { ...(home.policy?.min_eaches || {}), [code]: each.value.trim() },
    }))
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
    <t-navbar :fixed="false" :title="coinLabel(code)" />
    <t-notice-bar v-if="error" theme="error" :content="error" />
    <t-cell-group>
      <t-input v-model="count" label="个数" placeholder="不限制" align="right" />
      <t-input v-model="total" label="金额" placeholder="不限制" align="right" />
      <t-input v-model="each" label="人均" placeholder="不限制" align="right" />
    </t-cell-group>
    <div v-if="!hasTelegram()" class="bar-gap">
      <t-button theme="primary" block :loading="busy" @click="save">保存</t-button>
    </div>
  </main>
</template>
