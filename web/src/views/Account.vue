<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue"
import { useRoute, useRouter } from "vue-router"
import { api, grabbing, joining, type Home } from "../api"
import { haptic, setMain } from "../chrome"
import { applyHome, commitHome, home, loadHome } from "../state"
import { runFlow } from "../flow"
import Face from "../Face.vue"

const route = useRoute()
const router = useRouter()
const confirm = ref(false)
const error = ref("")
const busy = ref(false)

const person = computed(() => home.accounts.find((item) => String(item.user_id) === route.params.id && item.node_id === route.params.node))

if (!home.loaded) loadHome()

async function run(body: Record<string, unknown>) {
  const item = person.value
  if (!item) return
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

async function remove() {
  if (!person.value || busy.value) return
  busy.value = true
  setMain("确认删除", remove, true, true)
  error.value = ""
  try {
    await runFlow("正在删除", "已删除", async () => {
      applyHome(await api<Home>(`/api/accounts/${person.value!.user_id}`, { method: "DELETE" }), true)
    })
    setMain(null)
    router.replace("/")
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
    busy.value = false
    setMain("确认删除", remove, false, true)
  }
}

function ask() {
  confirm.value = true
  setMain("确认删除", remove, false, true)
}

onUnmounted(() => setMain(null))
</script>

<template>
  <main class="page">
    <t-navbar :fixed="false" title="账号" />
    <div v-if="!person" class="state">
      <t-empty :description="home.loading ? '正在加载' : '节点：账号不存在'" />
    </div>
    <template v-else>
      <t-notice-bar v-if="error" theme="error" :content="error" />
      <t-notice-bar v-else-if="person.reason" theme="error" :content="person.reason" />
      <header class="profile">
        <Face size="lg" :name="person.name" :peer="person.user_id" :mark="person.online ? 'on' : 'off'" />
        <h1 class="who">
          <b>{{ person.name }}</b>
          <span v-if="person.username">@{{ person.username.replace(/^@+/, "") }}</span>
        </h1>
      </header>
      <div class="ops ops-line">
        <button type="button" :class="{ on: grabbing(person) }" :aria-pressed="grabbing(person)" @click="run({ grab: !grabbing(person) })">抢包</button>
        <button type="button" :class="{ on: joining(person) }" :aria-pressed="joining(person)" @click="run({ join: !joining(person) })">加群</button>
      </div>
      <p v-if="person.brief && person.brief.state !== '会领'" class="brief-state">{{ person.brief.state }}</p>
      <t-cell-group class="facts" title="账号信息">
        <t-cell title="手机号" :note="person.phone || '无'" />
        <t-cell class="bio" title="简介" :description="person.about || '无'" />
        <t-cell title="生日" :note="person.birthday || '无'" />
        <t-cell title="语言" :note="person.lang || '无'" />
      </t-cell-group>
      <t-cell-group title="节点">
        <t-cell title="id" :note="String(person.user_id)" />
        <t-cell title="账号归属节点" :note="person.home || '未知'" />
        <t-cell title="分配节点" :note="person.city || '未知'" />
        <t-cell title="状态" :note="person.reason || (person.online ? '在线' : '离线')" />
      </t-cell-group>
      <t-cell-group title="标记">
        <t-cell title="NFT" :note="person.nft ? '有' : '无'" />
        <t-cell title="机构认证" :note="person.org === 'hold' || person.org === 'major' ? person.org : '无'" />
        <t-cell title="Premium会员" :note="person.premium ? '有' : '无'" />
        <t-cell title="官方认证" :note="person.verified ? '有' : '无'" />
        <t-cell v-if="person.restricted" title="受限" note="是" />
        <t-cell v-if="person.scam" title="诈骗" note="是" />
        <t-cell v-if="person.fake" title="虚假" note="是" />
      </t-cell-group>
      <div class="bar-gap">
        <t-button v-if="!confirm" theme="danger" variant="outline" block @click="ask">删除</t-button>
        <t-button v-else theme="danger" block :loading="busy" @click="remove">确认删除</t-button>
      </div>
      <p v-if="confirm" class="hint">删除后这个账号不再领取。</p>
    </template>
  </main>
</template>

<style scoped>
.facts :deep(.t-cell__note) {
  max-width: 62%;
  white-space: normal;
  text-align: right;
  overflow-wrap: anywhere;
}

.bio :deep(.t-cell__description) {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.45;
}

.brief-state {
  margin: 12px 0 0;
  padding: 12px 16px;
  background: #fff;
  color: rgba(0, 0, 0, 0.55);
  font-size: 15px;
  font-weight: 600;
  line-height: 1.3;
}
</style>
