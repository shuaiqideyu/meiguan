<script setup lang="ts">
import { computed, ref, watch } from "vue"
import { PEER_TABS, api, peerBucket, peerEmpty, peerLine, windowKey, type Entry } from "./api"
import Face from "./Face.vue"

const props = defineProps<{
  open: boolean
  selected: Array<string | number>
  keyOf: (item: Entry) => string | number
  locked?: (item: Entry) => boolean
  error?: string
}>()

const emit = defineEmits<{ toggle: [item: Entry] }>()

const query = ref("")
const filter = ref("all")
const dialogs = ref<Entry[]>([])
const resolved = ref<Entry | null>(null)
const loading = ref(false)
const looking = ref(false)
const listError = ref("")
const lookupError = ref("")
let listTicket = 0
let lookTicket = 0
let timer = 0

const examples = ["@name", "t.me/name", "-100…"]

watch(() => props.open, (on) => {
  if (!on) return
  query.value = ""
  filter.value = "all"
  resolved.value = null
  lookupError.value = ""
  listError.value = ""
  load()
}, { immediate: true })

watch(query, (value) => {
  window.clearTimeout(timer)
  lookupError.value = ""
  const text = value.trim()
  if (!props.open || !exactQuery(text)) {
    looking.value = false
    resolved.value = null
    lookTicket += 1
    return
  }
  timer = window.setTimeout(() => lookup(text), 400)
})

function exactQuery(raw: string) {
  const text = raw.trim()
  if (!text || text.includes(" ") || text.length > 200) return false
  const lower = text.toLowerCase()
  if (text.startsWith("@") && text.length >= 3) return true
  if (lower.includes("t.me/") || lower.includes("telegram.me/")) return true
  if (/^-100\d+$/.test(text) || /^-\d{6,}$/.test(text) || /^\d{6,}$/.test(text)) return true
  return /^[A-Za-z][A-Za-z0-9_]{2,31}$/.test(text)
}

function needle(raw: string) {
  const text = raw.trim()
  const lower = text.toLowerCase()
  if (/(?:t\.me|telegram\.me)\/(?:\+|joinchat\/)/.test(lower)) return ""
  if (/(?:t\.me|telegram\.me)\/c\//.test(lower)) return ""
  const link = lower.match(/(?:t\.me|telegram\.me)\/(?:s\/)?([a-z0-9_]+)/)
  if (link && link[1] !== "joinchat") return link[1]
  if (text.startsWith("@")) return text.slice(1).toLowerCase()
  return lower
}

function same(item: Entry | null) {
  return Boolean(item && resolved.value && windowKey(item) === windowKey(resolved.value))
}

const shown = computed(() => {
  const text = query.value.trim()
  const exact = exactQuery(text)
  const key = exact ? needle(text) : text.toLowerCase().replace(/^@/, "")
  const rows = dialogs.value.filter((item) => {
    if (filter.value !== "all" && peerBucket(item.kind) !== filter.value) return false
    if (!text) return true
    if (exact && !key) return same(item)
    const blob = `${item.title || ""} ${item.username || ""} ${item.chat_id || ""} ${item.user_id || ""}`.toLowerCase()
    return blob.includes(key)
  })
  if (resolved.value && !rows.some((item) => windowKey(item) === windowKey(resolved.value as Entry))) rows.unshift(resolved.value)
  return rows
})

const emptyText = computed(() => {
  if (exactQuery(query.value) && lookupError.value) return lookupError.value
  if (query.value.trim()) return "没有匹配"
  return peerEmpty(filter.value)
})

function mergeResolved() {
  const item = resolved.value
  if (!item) return
  if (!dialogs.value.some((row) => windowKey(row) === windowKey(item))) dialogs.value = [item, ...dialogs.value]
}

function load() {
  loading.value = true
  const mine = ++listTicket
  api<{ dialogs: Entry[] }>("/api/dialogs?bots=1")
    .then((data) => {
      if (mine !== listTicket) return
      dialogs.value = data.dialogs || []
      mergeResolved()
    })
    .catch((err) => {
      if (mine !== listTicket) return
      dialogs.value = resolved.value ? [resolved.value] : []
      listError.value = err instanceof Error ? err.message : "节点：没有读到窗口"
    })
    .finally(() => {
      if (mine === listTicket) loading.value = false
    })
}

function lookup(text: string) {
  const mine = ++lookTicket
  looking.value = true
  api<{ item?: Entry | null; message?: string }>(`/api/windows/find?q=${encodeURIComponent(text)}`)
    .then((data) => {
      if (mine !== lookTicket || query.value.trim() !== text) return
      resolved.value = data.item || null
      lookupError.value = data.item ? "" : (data.message || "")
      mergeResolved()
    })
    .catch((err) => {
      if (mine !== lookTicket) return
      resolved.value = null
      lookupError.value = err instanceof Error ? err.message : "节点：没有这个窗口"
    })
    .finally(() => {
      if (mine === lookTicket) looking.value = false
    })
}

function flushLookup() {
  window.clearTimeout(timer)
  const text = query.value.trim()
  if (exactQuery(text)) lookup(text)
}

function on(item: Entry) {
  return props.selected.includes(props.keyOf(item))
}

function pick(item: Entry) {
  if (props.locked?.(item)) return
  emit("toggle", item)
}

function faceId(item: Entry) {
  return item.kind === "bot" ? item.user_id : item.chat_id
}

function chosen(keys: string[], mark: (item: Entry) => string) {
  const want = new Set(keys)
  return dialogs.value.filter((item) => want.has(mark(item)))
}

function row(chatId: number) {
  return dialogs.value.find((item) => Number(item.chat_id) === chatId)
}

defineExpose({ chosen, row })
</script>

<template>
  <t-notice-bar v-if="error" theme="error" :content="error" />
  <t-notice-bar v-else-if="listError && !dialogs.length" theme="error" :content="listError" />
  <t-search v-model="query" placeholder="名称、@用户名、链接或数字 id" @submit="flushLookup" />
  <p class="hint">搜名称，或写 @用户名、链接、数字 id。</p>
  <ul class="pick-hints" aria-hidden="true">
    <li v-for="item in examples" :key="item">{{ item }}</li>
  </ul>
  <div class="peer-tabs" role="tablist" aria-label="对话类型">
    <button
      v-for="[value, label] in PEER_TABS"
      :key="value"
      type="button"
      role="tab"
      :class="{ on: filter === value }"
      :aria-selected="filter === value"
      @click="filter = value"
    >{{ label }}</button>
  </div>
  <div class="pick-scroll">
    <p v-if="loading" class="hint">正在加载</p>
    <p v-else-if="looking && !shown.length" class="hint">正在查找</p>
    <template v-else>
      <p v-if="looking" class="hint">正在查找</p>
      <t-empty v-if="!shown.length" :description="emptyText" />
      <t-cell
        v-for="item in shown"
        :key="windowKey(item)"
        :title="String(item.title || item.username || item.chat_id || item.user_id)"
        :description="peerLine(item)"
        :hover="!locked?.(item)"
        @click="pick(item)"
      >
        <template #left-icon><Face :name="String(item.title || item.username || '')" :peer="faceId(item)" /></template>
        <span v-if="locked?.(item)">已添加</span>
        <t-checkbox v-else class="mark" :checked="on(item)" />
      </t-cell>
    </template>
  </div>
</template>

<style scoped>
.pick-hints {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 16px 0;
  padding: 0;
  list-style: none;
}

.pick-hints li {
  padding: 2px 8px;
  border: 1px solid var(--td-gray-color-3, #e7e7e7);
  border-radius: 6px;
  background: #fff;
  color: var(--td-text-color-placeholder, rgba(0, 0, 0, 0.4));
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  line-height: 20px;
}
</style>
