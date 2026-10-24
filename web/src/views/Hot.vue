<script setup lang="ts">
import { computed, ref } from "vue"
import { api, clockText, orderWindows, peerLine, spanText, windowKey, type Entry } from "../api"
import WindowPick from "../WindowPick.vue"
import { haptic } from "../chrome"
import { home, loadHome, patch, takePolicy } from "../state"
import { runFlow } from "../flow"
import Face from "../Face.vue"
import Span from "../Span.vue"

defineProps<{ embedded?: boolean }>()

const packs = [
  ["xiehouyu", "歇后语"],
  ["riddle", "脑筋急转弯"],
  ["pun", "谐音梗"],
  ["joke", "段子"],
  ["flirt", "土味情话"],
  ["hello", "打招呼"],
  ["custom", "自定义"],
] as const

const low = ref("1800")
const high = ref("5400")
const burn = ref(false)
const openAt = ref("00:00")
const closeAt = ref("00:00")
const draftPacks = ref<string[]>([])
const draftLines = ref("")
const error = ref("")
const sheetError = ref("")
const open = ref(false)
const busy = ref(false)
const adding = ref(false)
const selected = ref<number[]>([])
const pick = ref<{ row: (chatId: number) => Entry | undefined } | null>(null)
const rank = ref<Record<string, string>>({})
let snapped = false

remember()

const hotWindows = computed(() => orderWindows(home.policy?.hot_windows || [], rank.value))

function remember() {
  if (snapped) return
  api<{ dialogs: Entry[] }>("/api/dialogs?bots=1")
    .then((data) => {
      if (snapped) return
      snapped = true
      const next: Record<string, string> = {}
      for (const item of data.dialogs || []) next[windowKey(item)] = item.active_at || ""
      rank.value = next
    })
    .catch(() => {})
}

function hotKey(item: Entry) {
  return Number(item.chat_id)
}

function openAdd() {
  if (!home.policy) return
  haptic()
  error.value = ""
  selected.value = (home.policy.hot_windows || []).map((item) => Number(item.chat_id)).filter(Boolean)
  adding.value = true
}

function toggleWindow(chatId?: number) {
  if (chatId == null) return
  if (selected.value.includes(chatId)) selected.value = selected.value.filter((item) => item !== chatId)
  else selected.value = [...selected.value, chatId]
}

async function saveWindows() {
  if (busy.value || !home.policy) return
  error.value = ""
  const windows = selected.value.map((id) => {
    const item = pick.value?.row(id)
      || (home.policy?.hot_windows || []).find((row) => Number(row.chat_id) === id)
    return {
      chat_id: id,
      kind: item?.kind || "",
      title: item?.title || "",
      username: item?.username || "",
      off: item?.off,
    }
  })
  const before = JSON.parse(JSON.stringify(home.policy))
  const kept = new Map((home.policy.hot_windows || []).map((item) => [Number(item.chat_id), item]))
  home.policy.hot_windows = windows.map((item) => {
    const old = kept.get(item.chat_id)
    return old ? { ...old, title: item.title || old.title, username: item.username || old.username, kind: item.kind || old.kind } : item
  })
  adding.value = false
  busy.value = true
  try {
    const data = await takePolicy("/api/hot/windows", {
      method: "POST",
      body: JSON.stringify({ windows }),
    }, true)
    if (data.message) error.value = data.message
  } catch (err) {
    home.policy = before
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
  }
}

if (!home.loaded) loadHome().then(fill)
else fill()

function fill() {
  if (!home.policy) return
  low.value = String(home.policy.hot_low)
  high.value = String(home.policy.hot_high)
  burn.value = (home.policy.hot_drop || 0) > 0
  openAt.value = home.policy.hot_open || "00:00"
  closeAt.value = home.policy.hot_close || "00:00"
  draftPacks.value = [...(home.policy.hot_packs || [])]
  draftLines.value = (home.policy.hot_lines || []).join("\n")
}

function fitSpan() {
  let nextLow = Number(low.value)
  let nextHigh = Number(high.value)
  if (!Number.isInteger(nextLow) || nextLow < 60) nextLow = 60
  if (nextLow > 86400) nextLow = 86400
  if (!Number.isInteger(nextHigh) || nextHigh < 60) nextHigh = 60
  if (nextHigh > 86400) nextHigh = 86400
  if (nextHigh < nextLow) nextHigh = nextLow
  low.value = String(nextLow)
  high.value = String(nextHigh)
  return { hot_low: nextLow, hot_high: nextHigh }
}

function hoursText() {
  const start = home.policy?.hot_open || "00:00"
  const end = home.policy?.hot_close || "00:00"
  if (start === end) return "全天"
  return `${start}–${end}`
}

function speakText() {
  const policy = home.policy
  if (!policy) return ""
  const drop = policy.hot_drop ? "自毁" : "不自毁"
  const ids = policy.hot_packs || []
  const packCount = ids.filter((id) => id !== "custom").length
  const custom = ids.includes("custom")
  const lines = (policy.hot_lines || []).length
  let copy = "未开文案"
  if (packCount && custom) copy = `文案 ${packCount} · 自定义 ${lines}`
  else if (packCount) copy = `文案 ${packCount}`
  else if (custom) copy = `自定义 ${lines}`
  return `${hoursText()} · ${spanText(policy.hot_low, policy.hot_high)} · ${drop} · ${copy}`
}

async function setMark(item: Entry, key: "sign" | "speak") {
  if (!home.policy || item.chat_id == null) return
  haptic()
  const next = !item[key]
  item[key] = next
  error.value = ""
  try {
    await patch({ [key === "sign" ? "hot_sign" : "hot_speak"]: { chat_id: item.chat_id, on: next } })
  } catch (err) {
    item[key] = !next
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

function samePacks(left: string[], right: string[]) {
  return left.length === right.length && left.every((item, index) => item === right[index])
}

function lineList(raw: string) {
  const seen: string[] = []
  for (const item of raw.split("\n")) {
    const text = item.trim().replace(/\s+/g, " ")
    if (!text || seen.includes(text)) continue
    seen.push(text)
  }
  return seen
}

function showConfig() {
  if (!home.policy) return
  haptic()
  fill()
  sheetError.value = ""
  open.value = true
}

async function saveSheet() {
  if (!home.policy || busy.value) return
  sheetError.value = ""
  const hot_open = clockText(openAt.value)
  const hot_close = clockText(closeAt.value)
  if (!hot_open || !hot_close) {
    sheetError.value = "时间写成 10:00 这样"
    return
  }
  const { hot_low, hot_high } = fitSpan()
  const hot_drop = burn.value ? 1 : 0
  const hot_packs = [...draftPacks.value]
  const hot_lines = lineList(draftLines.value)
  if (
    hot_open === (home.policy.hot_open || "00:00")
    && hot_close === (home.policy.hot_close || "00:00")
    && hot_low === home.policy.hot_low
    && hot_high === home.policy.hot_high
    && burn.value === ((home.policy.hot_drop || 0) > 0)
    && samePacks(hot_packs, home.policy.hot_packs || [])
    && samePacks(hot_lines, home.policy.hot_lines || [])
  ) {
    open.value = false
    return
  }
  busy.value = true
  try {
    await runFlow("正在保存", "已保存", () => patch({
      hot_open,
      hot_close,
      hot_low,
      hot_high,
      hot_drop,
      hot_packs,
      hot_lines,
    }))
    fill()
    open.value = false
  } catch (err) {
    sheetError.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
  }
}

function choosePack(id: string) {
  haptic()
  const prev = draftPacks.value
  draftPacks.value = prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
}

async function remove(chatId?: number) {
  if (!home.policy || chatId == null || busy.value) return
  const before = JSON.parse(JSON.stringify(home.policy))
  const chat_ids = home.policy.hot_windows.map((item) => item.chat_id).filter((item) => item !== chatId)
  home.policy.hot_windows = home.policy.hot_windows.filter((item) => Number(item.chat_id) !== chatId)
  error.value = ""
  busy.value = true
  try {
    await takePolicy("/api/hot/windows", { method: "POST", body: JSON.stringify({ chat_ids }) }, true)
  } catch (err) {
    home.policy = before
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
  }
}

defineExpose({ openAdd })
</script>

<template>
  <component :is="embedded ? 'section' : 'main'" :class="embedded ? '' : 'page'">
    <t-navbar :fixed="false" v-if="!embedded" title="自动炒群" />
    <t-notice-bar v-if="!adding && error" theme="error" :content="error" />
    <t-cell-group title="窗口">
      <t-empty v-if="!hotWindows.length" description="还没有窗口" />
      <div v-for="item in hotWindows" :key="item.chat_id" class="win">
        <Face :name="String(item.title || item.chat_id)" :peer="item.chat_id" />
        <div class="win-body">
          <div class="win-name">{{ item.title || item.chat_id }}</div>
          <div class="win-sub">{{ peerLine(item) }}</div>
          <div class="win-ops">
            <button type="button" class="chip" :class="{ on: item.sign }" :aria-pressed="item.sign ? 'true' : 'false'" @click="setMark(item, 'sign')">签到</button>
            <button type="button" class="chip" :class="{ on: item.speak }" :aria-pressed="item.speak ? 'true' : 'false'" @click="setMark(item, 'speak')">炒群</button>
          </div>
        </div>
        <t-button theme="danger" variant="text" size="small" @click="remove(item.chat_id)">移出</t-button>
      </div>
    </t-cell-group>
    <t-cell-group v-if="home.policy">
      <t-cell title="炒群配置" :description="speakText()" arrow hover @click="showConfig" />
    </t-cell-group>
    <t-popup v-model="adding" placement="bottom" :close-btn="true">
      <div class="sheet">
        <h2 class="sheet-title">添加窗口</h2>
        <WindowPick ref="pick" :open="adding" :error="error" :selected="selected" :key-of="hotKey" @toggle="(item) => toggleWindow(item.chat_id)" />
        <div class="sheet-bar">
          <t-button theme="primary" block :disabled="busy" @click="saveWindows">保存</t-button>
        </div>
      </div>
    </t-popup>
    <t-popup v-model="open" placement="bottom" :close-btn="true">
      <div class="sheet">
        <h2 class="sheet-title">炒群配置</h2>
        <t-notice-bar v-if="sheetError" theme="error" :content="sheetError" />
        <p class="hint">北京时间。开始和结束相同就是全天，可以跨过零点，例如 11:00-10:00。这段以外不发言。</p>
        <div class="tune">
          <div class="tune-row">
            <span class="tune-name">炒群时段</span>
            <span class="clocks">
              <input v-model="openAt" type="time" aria-label="开始" />
              <i>-</i>
              <input v-model="closeAt" type="time" aria-label="结束" />
            </span>
          </div>
          <div class="tune-row">
            <Span class="delay" label="发言延迟" v-model:low="low" v-model:high="high" :min="60" :max="86400" />
            <span class="burn">
              <span>发言自毁</span>
              <t-switch :model-value="burn" @change="burn = Boolean($event)" />
            </span>
          </div>
        </div>
        <p class="sheet-label">文案</p>
        <div class="packs" role="group" aria-label="文案">
          <button
            v-for="[id, label] in packs"
            :key="id"
            type="button"
            class="pack"
            :class="{ on: draftPacks.includes(id) }"
            :aria-pressed="draftPacks.includes(id)"
            @click="choosePack(id)"
          >{{ label }}</button>
        </div>
        <template v-if="draftPacks.includes('custom')">
          <p class="hint">一行一句，最多 50 句，一句最长 80 字。</p>
          <t-cell-group>
            <t-textarea v-model="draftLines" placeholder="一行一句" :autosize="{ minRows: 4, maxRows: 8 }" />
          </t-cell-group>
        </template>
        <div class="sheet-bar">
          <t-button theme="primary" block :loading="busy" @click="saveSheet">保存</t-button>
        </div>
      </div>
    </t-popup>
  </component>
</template>

<style scoped>
.packs {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(6.8em, 1fr));
  gap: 8px;
  padding: 8px 16px 12px;
  background: #fff;
  font-size: 14px;
}

.pack {
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  min-height: 40px;
  margin: 0;
  padding: 8px 6px;
  border: 1px solid var(--td-gray-color-3, #e7e7e7);
  border-radius: 8px;
  background: #fff;
  color: var(--td-text-color-primary, rgba(0, 0, 0, 0.9));
  font-size: inherit;
  line-height: 1.2;
  white-space: nowrap;
}

.pack.on {
  border-color: #2ba471;
  background: #e8f8f2;
  color: #2ba471;
  font-weight: 600;
}

.tune {
  margin-top: 12px;
  background: #fff;
}

.tune-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 48px;
  padding: 8px 16px;
}

.tune-row + .tune-row {
  box-shadow: inset 0 1px 0 var(--td-gray-color-3, #e7e7e7);
}

.tune-name {
  flex: none;
  font-size: 16px;
  line-height: 1.2;
  white-space: nowrap;
}

.clocks {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  min-width: 0;
}

.clocks input {
  width: 5.4em;
  height: 32px;
  margin: 0;
  padding: 0 4px;
  box-sizing: border-box;
  border: 1px solid var(--td-gray-color-3, #e7e7e7);
  border-radius: 6px;
  background: #fff;
  color: inherit;
  font-size: 16px;
  font-variant-numeric: tabular-nums;
  text-align: center;
}

.clocks input::-webkit-calendar-picker-indicator {
  display: none;
}

.clocks i {
  flex: none;
  color: var(--td-text-color-secondary, rgba(0, 0, 0, 0.6));
  font-style: normal;
}

.delay {
  flex: 1 1 auto;
  min-width: 0;
}

.tune-row :deep(.span-row) {
  min-height: 0;
  padding: 0;
  gap: 8px;
  justify-content: flex-start;
}

.tune-row :deep(.span-name) {
  white-space: nowrap;
}

.tune-row :deep(.span-boxes) {
  flex: 1 1 auto;
  justify-content: flex-end;
  min-width: 0;
}

.tune-row :deep(.span-boxes input) {
  width: clamp(46px, 13vw, 68px);
  padding: 0 2px;
}

.burn {
  display: flex;
  flex: none;
  align-items: center;
  gap: 6px;
  font-size: 16px;
  line-height: 1.2;
  white-space: nowrap;
}

.sheet-label {
  margin: 14px 16px 0;
  color: var(--td-text-color-secondary, rgba(0, 0, 0, 0.6));
  font-size: 13px;
  line-height: 1.4;
}


.win {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 8px 12px 16px;
  background: #fff;
  box-shadow: inset 0 -1px 0 var(--td-gray-color-3, #e7e7e7);
}

.win-body {
  flex: 1;
  min-width: 0;
}

.win-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 16px;
  line-height: 1.4;
}

.win-sub {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--td-text-color-secondary, rgba(0, 0, 0, 0.6));
  font-size: 13px;
  line-height: 1.4;
}

.win-ops {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.chip {
  flex: none;
  margin: 0;
  min-height: 32px;
  padding: 6px 12px;
  border: 1px solid var(--td-gray-color-3, #e7e7e7);
  border-radius: 8px;
  background: #fff;
  color: var(--td-text-color-secondary, rgba(0, 0, 0, 0.6));
  font-size: 13px;
  line-height: 1.2;
}

.chip.on {
  border-color: #2ba471;
  background: #e8f8f2;
  color: #2ba471;
  font-weight: 600;
}
</style>
