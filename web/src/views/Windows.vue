<script setup lang="ts">
import { computed, ref, watch } from "vue"
import { AXIS, api, orderWindows, peerLine, windowKey, type BlockUser, type Entry, type Policy } from "../api"
import WindowPick from "../WindowPick.vue"
import { haptic } from "../chrome"
import { home, loadHome, noteWrite, takePolicy } from "../state"
import Face from "../Face.vue"

const props = defineProps<{ live?: boolean }>()
const selected = ref<string[]>([])
const error = ref("")
const adding = ref(false)
const pick = ref<{ chosen: (keys: string[], mark: (item: Entry) => string) => Entry[] } | null>(null)
const busy = ref(false)
const confirming = ref(false)
const modes = AXIS.map(([value, label]) => ({ value, label }))
const hints: Record<string, string> = {
  off: "关闭：先不限制，窗口记在这一档。",
  white: "白名单：只抢下面这些。",
  black: "黑名单：下面这些不抢。",
}
const rank = ref<Record<string, string>>({})
let snapped = false
let desired = ""
let acknowledged = ""
let saving = false
if (!home.loaded) loadHome()
watch(() => props.live, (on) => {
  if (on) remember()
}, { immediate: true })

function remember() {
  if (snapped) return
  api<{ dialogs: Entry[] }>("/api/dialogs?bots=1", {}, true)
    .then((data) => {
      if (snapped) return
      snapped = true
      const next: Record<string, string> = {}
      for (const item of data.dialogs || []) next[windowKey(item)] = item.active_at || ""
      rank.value = next
    })
    .catch(() => {})
}

const listedWindows = computed(() => orderWindows(home.policy?.windows || [], rank.value))
const modeHint = computed(() => hints[home.policy?.window_mode || "off"] || "")

function applyLocal(value: string) {
  if (!home.policy) return
  home.policy.window_mode = value
  home.policy.bot_mode = value
  const lists = home.policy.window_lists
  if (lists && value in lists) home.policy.windows = lists[value] || []
}

function mark(item: Entry) {
  return item.kind === "bot" ? `bot:${item.user_id}` : `chat:${item.chat_id}`
}

function same(row: Entry, item: Entry) {
  const rowBot = (row.kind || "chat") === "bot"
  const itemBot = item.kind === "bot"
  if (rowBot !== itemBot) return false
  const rowName = (row.username || "").toLowerCase()
  const itemName = (item.username || "").toLowerCase()
  if (rowName && rowName === itemName) return true
  const rowId = Number(rowBot ? row.user_id : row.chat_id)
  const itemId = Number(itemBot ? item.user_id : item.chat_id)
  return Boolean(rowId && itemId && rowId === itemId)
}

function listed(item: Entry) {
  return (home.policy?.windows || []).some((row) => same(row, item))
}

function openAdd() {
  haptic()
  error.value = ""
  confirming.value = false
  selected.value = []
  adding.value = true
}

function toggle(item: Entry) {
  if (listed(item)) return
  const key = mark(item)
  if (selected.value.includes(key)) {
    selected.value = selected.value.filter((id) => id !== key)
    return
  }
  selected.value = [...selected.value, key]
}

async function flushMode() {
  if (saving) return
  saving = true
  try {
    while (home.policy && desired && desired !== acknowledged) {
      const sending = desired
      noteWrite()
      const data = await api<{ policy: Policy; offline?: string[] }>("/api/policy", {
        method: "PATCH",
        body: JSON.stringify({ window_mode: sending }),
      }, true)
      noteWrite()
      if (data.offline) home.offline = data.offline
      acknowledged = sending
      if (!home.policy) break
      if (desired === sending) home.policy = data.policy
      else {
        home.policy.window_lists = data.policy.window_lists
        applyLocal(desired)
      }
    }
  } catch (err) {
    desired = acknowledged
    applyLocal(acknowledged)
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    saving = false
    if (home.policy && desired !== acknowledged) void flushMode()
  }
}

async function mode(payload: { value?: string | number } | string | number) {
  const value = typeof payload === "object" ? String(payload.value) : String(payload)
  if (!home.policy || !hints[value]) return
  if (value === (desired || home.policy.window_mode)) return
  haptic()
  error.value = ""
  confirming.value = false
  noteWrite()
  if (!saving) acknowledged = home.policy.window_mode
  desired = value
  applyLocal(value)
  void flushMode()
}

function rememberPolicy() {
  return home.policy ? JSON.parse(JSON.stringify(home.policy)) as Policy : null
}

async function add() {
  if (!selected.value.length || busy.value || !home.policy) return
  const picks = selected.value.flatMap((key) => {
    const item = pick.value?.chosen([key], mark)?.[0]
    if (!item) return []
    const title = item.title || ""
    const username = item.username || ""
    if (item.kind === "bot") return [{ kind: "bot", user_id: Number(item.user_id), title, username }]
    return [{ kind: item.kind || "chat", chat_id: Number(item.chat_id), title, username }]
  })
  if (!picks.length) return
  error.value = ""
  const before = rememberPolicy()
  const mode = home.policy.window_mode || "off"
  const lists = { ...(home.policy.window_lists || {}) }
  const current = [...(lists[mode] || home.policy.windows || [])]
  for (const pick of picks) {
    current.push({
      id: `new-${pick.kind}-${pick.user_id || pick.chat_id}`,
      kind: pick.kind,
      title: pick.title,
      username: pick.username,
      ...(pick.kind === "bot" ? { user_id: pick.user_id } : { chat_id: pick.chat_id }),
    })
  }
  lists[mode] = current
  home.policy.window_lists = lists
  home.policy.windows = current
  selected.value = []
  adding.value = false
  busy.value = true
  try {
    const data = await takePolicy("/api/entries", { method: "POST", body: JSON.stringify({ axis: "window", picks }) }, true)
    if (data.message) error.value = data.message
  } catch (err) {
    if (before) home.policy = before
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
  }
}

async function remove(id?: string) {
  if (!id || busy.value || !home.policy) return
  error.value = ""
  const before = rememberPolicy()
  const mode = home.policy.window_mode || "off"
  const lists = { ...(home.policy.window_lists || {}) }
  if (lists[mode]) lists[mode] = lists[mode].filter((item) => item.id !== id)
  home.policy.window_lists = lists
  home.policy.windows = home.policy.windows.filter((item) => item.id !== id)
  busy.value = true
  try {
    await takePolicy("/api/entries", { method: "DELETE", body: JSON.stringify({ axis: "window", id }) }, true)
  } catch (err) {
    if (before) home.policy = before
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
  }
}

async function clear() {
  if (!confirming.value) {
    confirming.value = true
    return
  }
  if (busy.value || !home.policy) return
  error.value = ""
  const before = rememberPolicy()
  const mode = home.policy.window_mode || "off"
  const lists = { ...(home.policy.window_lists || {}) }
  lists[mode] = []
  home.policy.window_lists = lists
  home.policy.windows = []
  confirming.value = false
  busy.value = true
  try {
    await takePolicy("/api/entries/clear", { method: "POST", body: JSON.stringify({ axis: "window" }) }, true)
  } catch (err) {
    if (before) home.policy = before
    confirming.value = true
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
  }
}

const keywordText = ref("")
const minuteText = ref("")
const keywordPopup = ref(false)
const userRows = ref<BlockUser[]>([])
const userDraft = ref("")
const textDraft = ref("")

function copyUsers(rows: BlockUser[] | undefined): BlockUser[] {
  return (rows || []).map((row) => ({
    user_id: Number(row.user_id || 0),
    username: String(row.username || ""),
    texts: [...(row.texts || [])],
  }))
}

function userKey(row: BlockUser) {
  return row.user_id ? `id:${row.user_id}` : `name:${String(row.username || "").toLowerCase()}`
}

function userLabel(row: BlockUser) {
  return row.username ? `@${row.username}` : String(row.user_id || "")
}

function readWho(raw: string): BlockUser | string {
  const bare = raw.trim().replace(/^@/, "")
  if (!bare) return "用户写成 @用户名或数字 id"
  if (/^\d+$/.test(bare)) {
    const id = Number(bare)
    if (!Number.isSafeInteger(id) || id <= 0) return "用户写成 @用户名或数字 id"
    return { user_id: id, username: "", texts: [] }
  }
  if (!/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(bare)) return "用户写成 @用户名或数字 id"
  return { user_id: 0, username: bare, texts: [] }
}

function readTexts(raw: string): string[] | string {
  const parts = [...new Set(raw.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean))]
  if (!parts.length) return "内容不能空"
  if (parts.some((s) => s.length > 80)) return "单条内容请写在 80 字以内"
  if (parts.length > 20) return "每个用户的内容请写在 20 条以内"
  return parts
}

function takeDraft(): string {
  if (!userDraft.value.trim() && !textDraft.value.trim()) return ""
  const who = readWho(userDraft.value)
  if (typeof who === "string") return who
  const texts = readTexts(textDraft.value)
  if (typeof texts === "string") return texts
  const key = userKey(who)
  const next = userRows.value.map((row) => ({ ...row, texts: [...row.texts] }))
  const found = next.find((row) => userKey(row) === key)
  if (found) {
    const seen = new Set(found.texts)
    for (const piece of texts) {
      if (seen.has(piece)) continue
      if (found.texts.length >= 20) return "每个用户的内容请写在 20 条以内"
      found.texts.push(piece)
      seen.add(piece)
    }
    if (who.username && !found.username) found.username = who.username
  } else {
    if (next.length >= 100) return "用户请写在 100 个以内"
    next.push({ user_id: who.user_id, username: who.username, texts })
  }
  userRows.value = next
  userDraft.value = ""
  textDraft.value = ""
  return ""
}

function addUser() {
  haptic()
  if (!userDraft.value.trim() && !textDraft.value.trim()) {
    error.value = "用户写成 @用户名或数字 id"
    return
  }
  error.value = takeDraft()
}

function removeUser(index: number) {
  haptic()
  userRows.value = userRows.value.filter((_, at) => at !== index)
}

function openKeywords() {
  haptic()
  error.value = ""
  keywordText.value = (home.policy?.window_block_keywords || []).join(",")
  minuteText.value = String(home.policy?.window_block_minutes ?? "")
  userRows.value = copyUsers(home.policy?.window_block_users)
  userDraft.value = ""
  textDraft.value = ""
  keywordPopup.value = true
}
watch(() => home.policy?.window_block_keywords, (v) => {
  if (v) keywordText.value = v.join(",")
}, { immediate: true })
watch(() => home.policy?.window_block_minutes, (v) => {
  if (v !== undefined && v !== null) minuteText.value = String(v)
}, { immediate: true })
watch(() => home.policy?.window_block_users, (v) => {
  if (!keywordPopup.value) userRows.value = copyUsers(v)
}, { immediate: true })

async function saveKeywords() {
  if (!home.policy) return
  haptic()
  error.value = ""
  const draftError = takeDraft()
  if (draftError) {
    error.value = draftError
    return
  }
  const keywords = keywordText.value.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean)
  const minutes = Math.max(0, Math.min(parseInt(minuteText.value, 10) || 0, 1440))
  const users = userRows.value.map((row) => {
    const item: BlockUser = { texts: row.texts }
    if (row.user_id) item.user_id = row.user_id
    if (row.username) item.username = row.username
    return item
  })
  noteWrite()
  try {
    const data = await api<{ policy: Policy }>("/api/policy", {
      method: "PATCH",
      body: JSON.stringify({
        window_block_keywords: keywords,
        window_block_minutes: minutes,
        window_block_users: users,
      }),
    }, true)
    noteWrite()
    if (home.policy && data.policy) home.policy = data.policy
    keywordPopup.value = false
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

defineExpose({ openAdd })
</script>

<template>
  <section v-if="home.policy">
    <t-notice-bar v-if="!adding && error" theme="error" :content="error" />
    <div class="mode">
      <t-segmented :value="home.policy.window_mode" block :options="modes" @change="mode" />
      <p class="hint">{{ modeHint }}</p>
    </div>
    <div class="bar-gap">
      <t-button block @click="openKeywords">临时屏蔽</t-button>
    </div>
    <t-cell-group title="窗口">
      <t-empty v-if="!home.policy.windows.length" description="还没有窗口" />
      <t-cell
        v-for="item in listedWindows"
        :key="item.id"
        :title="String(item.title || item.username || item.chat_id)"
        :description="peerLine(item)"
      >
        <template #left-icon><Face :name="String(item.title || item.username || '')" :peer="item.kind === 'bot' ? item.user_id : item.chat_id" /></template>
        <t-button theme="danger" variant="text" size="small" @click="remove(item.id)">移出</t-button>
      </t-cell>
    </t-cell-group>
    <div v-if="home.policy.windows.length" class="bar-gap">
      <t-button theme="danger" variant="outline" block @click="clear">{{ confirming ? "确认清空" : "清空名单" }}</t-button>
    </div>
    <t-popup v-model="adding" placement="bottom" :close-btn="true">
      <div class="sheet">
        <h2 class="sheet-title">添加窗口</h2>
        <WindowPick ref="pick" :open="adding" :error="error" :selected="selected" :key-of="mark" :locked="listed" @toggle="toggle" />
        <div class="sheet-bar">
          <t-button theme="primary" block :disabled="!selected.length || busy" @click="add">添加</t-button>
        </div>
      </div>
    </t-popup>
    <t-popup v-model="keywordPopup" placement="bottom" :close-btn="true">
      <div class="sheet">
        <h2 class="sheet-title">临时屏蔽</h2>
        <t-notice-bar v-if="error" theme="error" :content="error" />
        <t-cell-group>
          <t-cell title="关键词" description="命中后屏蔽窗口，逗号分隔">
            <t-textarea v-model="keywordText" placeholder="测挂,封禁,维护" :autosize="{ minRows: 2, maxRows: 4 }" />
          </t-cell>
          <t-cell title="屏蔽时长（分钟）" description="0 表示关闭；重复命中只刷新，不叠加">
            <t-input v-model="minuteText" type="number" placeholder="30" />
          </t-cell>
        </t-cell-group>
        <t-cell-group title="指定用户">
          <t-cell
            v-for="(row, index) in userRows"
            :key="userKey(row)"
            :title="userLabel(row)"
            :description="row.texts.join('，')"
          >
            <t-button theme="danger" variant="text" size="small" @click="removeUser(index)">移出</t-button>
          </t-cell>
          <t-cell title="用户">
            <t-input v-model="userDraft" placeholder="@用户名或 id" />
          </t-cell>
          <t-cell title="内容" description="含这段就屏蔽，逗号分隔">
            <t-input v-model="textDraft" placeholder="hb" />
          </t-cell>
        </t-cell-group>
        <div class="sheet-bar row">
          <t-button variant="outline" block @click="addUser">添加</t-button>
          <t-button theme="primary" block @click="saveKeywords">保存</t-button>
        </div>
      </div>
    </t-popup>
  </section>
</template>
