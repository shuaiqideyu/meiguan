<script setup lang="ts">
import { onUnmounted, ref } from "vue"
import { useRoute, useRouter } from "vue-router"
import { MODULES, hasTelegram } from "../api"
import { haptic, setMain } from "../chrome"
import { home, loadHome, patch } from "../state"
import { runFlow } from "../flow"

const route = useRoute()
const router = useRouter()
const low = ref("0")
const high = ref("0")
const sayLow = ref("0")
const sayHigh = ref("0")
const error = ref("")
const busy = ref(false)
const kind = String(route.params.kind)
const label = MODULES.find(([name]) => name === kind)?.[1] || "延迟"

if (!home.loaded) loadHome().then(fill)
else fill()

function showDelay(value = 0) {
  if (value > 0 && value < 1) return `${Math.round(value * 1000)}ms`
  return String(value)
}

function readDelay(text: string) {
  const raw = text.trim().toLowerCase().replace(/\s+/g, "")
  if (!raw || raw === "0") return 0
  const fromMs = raw.endsWith("ms")
  const body = raw.replace(/ms$/, "")
  if (!/^\d+(\.\d+)?$/.test(body)) return null
  const number = fromMs ? Number(body) / 1000 : Number(body)
  if (!Number.isFinite(number) || number < 0 || number > 60) return null
  return number
}

function fill() {
  const span = home.policy?.mode_delays?.[kind]
  low.value = span ? showDelay(span.low) : "0"
  high.value = span ? showDelay(span.high) : "0"
  const say = home.policy?.phrase_say
  sayLow.value = say ? showDelay(say.low) : "0"
  sayHigh.value = say ? showDelay(say.high) : "0"
}

async function save() {
  const span = { low: readDelay(low.value), high: readDelay(high.value) }
  const say = kind === "phrase" ? { low: readDelay(sayLow.value), high: readDelay(sayHigh.value) } : null
  if (span.low == null || span.high == null || span.low > span.high || (say && (say.low == null || say.high == null || say.low > say.high))) {
    error.value = "延迟请写在 0 到 60 秒，可以写 0.3 或 500ms"
    return
  }
  busy.value = true
  error.value = ""
  setMain("保存", save, true)
  try {
    await runFlow("正在保存", "已保存", () => patch({
      mode_delays: { [kind]: span },
      ...(say ? { phrase_say: say } : {}),
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

const canBurn = kind === "phrase"

async function setBurn(on: boolean) {
  if (!canBurn) return
  haptic()
  const prev = !!home.policy?.burns?.phrase
  if (home.policy) {
    home.policy.burns = { ...(home.policy.burns || {}), phrase: on }
  }
  error.value = ""
  try {
    await patch({ burns: { phrase: on } })
  } catch (err) {
    if (home.policy) home.policy.burns = { ...(home.policy.burns || {}), phrase: prev }
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}
</script>

<template>
  <main class="page">
    <t-navbar :fixed="false" :title="label" />
    <t-notice-bar v-if="error" theme="error" :content="error" />
    <t-cell-group>
      <label class="delay-row">
        <span>延迟</span>
        <span class="delay-boxes">
          <input v-model="low" inputmode="decimal" autocomplete="off" aria-label="起" />
          <i>-</i>
          <input v-model="high" inputmode="decimal" autocomplete="off" aria-label="止" />
        </span>
      </label>
      <label v-if="kind === 'phrase'" class="delay-row">
        <span>说出之前</span>
        <span class="delay-boxes">
          <input v-model="sayLow" inputmode="decimal" autocomplete="off" aria-label="说出起" />
          <i>-</i>
          <input v-model="sayHigh" inputmode="decimal" autocomplete="off" aria-label="说出止" />
        </span>
      </label>
    </t-cell-group>
    <p class="hint">{{ kind === "phrase" ? "0 到 60 秒。可以写 0.3 或 500ms。两个数一样就是固定等待。延迟是看到后先等再动手。说出之前是把口令打出去之前再等。" : "0 到 60 秒。可以写 0.3 或 500ms。两个数一样就是固定等待。" }}</p>
    <t-cell-group v-if="canBurn">
      <t-cell title="消息自毁">
        <t-switch :model-value="!!home.policy?.burns?.phrase" @change="setBurn" />
      </t-cell>
    </t-cell-group>
    <div v-if="!hasTelegram()" class="bar-gap">
      <t-button theme="primary" block :loading="busy" @click="save">保存</t-button>
    </div>
  </main>
</template>

<style scoped>
.delay-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 48px;
  padding: 8px 16px;
  background: #fff;
}

.delay-row span {
  flex: none;
  font-size: 16px;
}

.delay-boxes {
  display: flex;
  align-items: center;
  gap: 8px;
}

.delay-row input {
  width: 76px;
  height: 32px;
  border: 0;
  border-radius: 8px;
  background: #f3f3f3;
  font-variant-numeric: tabular-nums;
  text-align: center;
  font-size: 15px;
}

.delay-row i {
  color: rgba(0, 0, 0, 0.4);
  font-style: normal;
}
</style>
