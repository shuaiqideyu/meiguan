<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { useRouter } from "vue-router"
import { CloseIcon, FileZipIcon, QrcodeIcon, UserAddIcon, VerifyIcon } from "tdesign-icons-vue-next"
import { api, grabbing, joining, watching, type Account, type Home } from "../api"
import { haptic, tg } from "../chrome"
import { applyHome, commitHome, home, loadHome } from "../state"
import { runFlow } from "../flow"
import Face from "../Face.vue"
import Import from "./Import.vue"

const router = useRouter()
const adding = ref(false)
const fan = ref(false)
const notice = ref(false)
const token = ref("")
const error = ref("")
const importer = ref<{ begin: (kind: "code" | "qr" | "pack") => void } | null>(null)
const ways = [
  { kind: "qr" as const, label: "二维码", icon: QrcodeIcon, x: "-22px", y: "-124px" },
  { kind: "code" as const, label: "验证码", icon: VerifyIcon, x: "-104px", y: "-62px" },
  { kind: "pack" as const, label: "压缩包", icon: FileZipIcon, x: "-122px", y: "4px" },
]

const listed = computed(() =>
  [...home.accounts].sort((a, b) => {
    const left = a.active_at || ""
    const right = b.active_at || ""
    if (left !== right) {
      if (!left) return 1
      if (!right) return -1
      return right < left ? -1 : 1
    }
    return a.user_id - b.user_id
  }),
)

onMounted(() => {
  if (!home.loaded) loadHome()
})

function openNotice() {
  haptic()
  token.value = ""
  error.value = ""
  notice.value = true
}

function openFather(event: MouseEvent) {
  event.preventDefault()
  haptic()
  const url = "https://t.me/BotFather"
  const web = tg()
  if (web?.openTelegramLink) {
    web.openTelegramLink(url)
    return
  }
  window.open(url, "_blank", "noopener")
}

async function saveNotice() {
  const value = token.value.trim()
  if (!value) {
    error.value = "请填写 token"
    return
  }
  haptic()
  error.value = ""
  try {
    await runFlow("正在保存", "已保存", async () => {
      const data = await api<{ notify: { bound?: boolean; username?: string } }>("/api/notify", {
        method: "POST",
        body: JSON.stringify({ token: value }),
      })
      home.notify = {
        bound: !!data.notify?.bound,
        username: String(data.notify?.username || "").replace(/^@+/, ""),
      }
      token.value = ""
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

async function testNotice() {
  haptic()
  error.value = ""
  if (!token.value.trim() && !home.notify.bound) {
    error.value = "请填写 token"
    return
  }
  try {
    if (token.value.trim()) await saveNotice()
    if (error.value || !home.notify.bound) return
    await runFlow("正在测试", "已发到私聊", async () => {
      const data = await api<{ message?: string }>("/api/notify/test", { method: "POST", body: "{}" })
      return data.message || "已发到私聊"
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

async function clearNotice() {
  haptic()
  error.value = ""
  try {
    await runFlow("正在停用", "已停用", async () => {
      applyHome(await api<Home>("/api/notify", { method: "DELETE" }), true)
      token.value = ""
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

function toggleFan() {
  haptic()
  fan.value = !fan.value
}

function pickWay(kind: "code" | "qr" | "pack") {
  fan.value = false
  importer.value?.begin(kind)
}

async function run(item: Account, body: Record<string, unknown>) {
  haptic()
  error.value = ""
  const prevPaused = item.paused
  const prevJoin = item.join
  if ("grab" in body) item.paused = !body.grab
  if ("join" in body) item.join = !!body.join
  try {
    await commitHome(() => api<Home>("/api/accounts/run", {
      method: "POST",
      body: JSON.stringify({ user_id: item.user_id, ...body }),
    }, true))
  } catch (err) {
    item.paused = prevPaused
    item.join = prevJoin
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}
</script>

<template>
  <main class="page dock-page has-fab">
    <t-navbar :fixed="false" title="账号列表" :safe-area-inset-top="false" />
    <p v-if="home.loading && !home.accounts.length && !home.error" class="hint">正在加载</p>
    <div v-else-if="home.error && !home.accounts.length" class="state">
      <t-empty :description="home.error" />
      <t-button theme="primary" @click="loadHome">重试</t-button>
    </div>
    <template v-else>
      <t-notice-bar v-if="error && !notice" theme="error" :content="error" />
      <t-cell-group>
        <t-cell
          title="通知机器人"
          :description="home.notify.bound ? `@${home.notify.username}` : '未添加'"
          hover
          @click="openNotice"
        >
          <template #note>
            <t-button size="small" theme="primary" variant="text" @click.stop="openNotice">
              {{ home.notify.bound ? "管理" : "添加" }}
            </t-button>
          </template>
        </t-cell>
      </t-cell-group>
      <t-cell-group v-if="home.accounts.length">
        <div v-for="item in listed" :key="item.user_id" class="account">
          <t-cell arrow hover @click="router.push(`/account/${item.node_id}/${item.user_id}`)">
            <template #left-icon>
              <Face :name="item.name" :peer="item.user_id" :mark="item.online ? 'on' : 'off'" />
            </template>
            <template #title>
              <span class="account-name">
                <b>{{ item.name }}</b>
                <span v-if="item.username">@{{ item.username.replace(/^@+/, "") }}</span>
              </span>
            </template>
            <template #description>{{ item.active || "无" }}</template>
            <template #note>
              <div class="ops" @click.stop>
                <button type="button" :class="{ on: grabbing(item) }" :aria-pressed="grabbing(item)" @click="run(item, { grab: !grabbing(item) })">抢包</button>
                <button type="button" :class="{ on: joining(item) }" :aria-pressed="joining(item)" @click="run(item, { join: !joining(item) })">加群</button>
              </div>
            </template>
          </t-cell>
          <p v-if="item.reason" class="reason">{{ item.reason }}</p>
        </div>
      </t-cell-group>
      <t-empty v-else description="还没有账号" />
    </template>
    <div class="account-fab">
      <button v-if="fan" type="button" class="account-fab-scrim" aria-label="关闭" @click="fan = false"></button>
      <div class="account-fab-stack">
        <template v-if="fan">
          <button
            v-for="(item, index) in ways"
            :key="item.kind"
            type="button"
            class="account-fab-item"
            :style="{ '--x': item.x, '--y': item.y, '--i': String(ways.length - 1 - index) }"
            @click="pickWay(item.kind)"
          >
            <component :is="item.icon" size="18px" />
            <span>{{ item.label }}</span>
          </button>
        </template>
        <button
          type="button"
          class="account-fab-main"
          :class="{ on: fan }"
          :aria-expanded="fan"
          aria-label="添加账号"
          @click="toggleFan"
        >
          <CloseIcon v-if="fan" size="24px" />
          <UserAddIcon v-else size="26px" />
        </button>
      </div>
    </div>
    <Import ref="importer" v-model:show="adding" />
    <t-popup v-model="notice" placement="bottom" :close-btn="true">
      <div class="sheet">
        <h2 class="sheet-title">通知机器人</h2>
        <p class="hint one">打开 <a href="https://t.me/BotFather" @click="openFather">@BotFather</a>，发送 /newbot，创建后获取 token 即可。</p>
        <t-notice-bar v-if="error" theme="error" :content="error" />
        <label class="api-field">
          <span>HTTP API</span>
          <t-input v-model="token" type="password" placeholder="粘贴到这里" borderless />
        </label>
        <div class="sheet-actions">
          <t-button theme="primary" @click="saveNotice">保存</t-button>
          <t-button theme="primary" variant="outline" @click="testNotice">测试</t-button>
          <t-button v-if="home.notify.bound" theme="danger" variant="outline" @click="clearNotice">停用</t-button>
        </div>
        <p class="hint one">勾选后，听到红包就私信，不点。</p>
        <p v-if="!home.notify.bound" class="hint one">先添加通知机器人，再选账号。</p>
        <div v-if="listed.length" class="notice-accounts">
          <div v-for="item in listed" :key="item.user_id" class="notice-row">
            <Face size="sm" :name="item.name" :peer="item.user_id" :mark="item.online ? 'on' : 'off'" />
            <span>
              <b>{{ item.name }}</b>
              <i v-if="item.username">@{{ item.username.replace(/^@+/, "") }}</i>
            </span>
            <t-switch :model-value="watching(item)" :disabled="!home.notify.bound" @change="run(item, { watch: !watching(item) })" />
          </div>
        </div>
        <p v-else class="hint one">还没有账号</p>
      </div>
    </t-popup>
  </main>
</template>

<style scoped>
.notice-accounts {
  max-height: 240px;
  margin-top: 8px;
  overflow: auto;
  border-top: 1px solid rgba(0, 0, 0, 0.06);
}

.notice-row {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 56px;
  padding: 6px 0;
}

.notice-row :deep(.t-switch) {
  flex: none;
  margin-left: auto;
}

.notice-row span {
  min-width: 0;
  font-size: 16px;
  line-height: 1.3;
}

.notice-row b,
.notice-row i {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.notice-row i {
  color: rgba(0, 0, 0, 0.45);
  font-style: normal;
  font-size: 13px;
  font-weight: 400;
}
</style>
