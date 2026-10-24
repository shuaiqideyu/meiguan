<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { api } from "../api"
import { haptic, tg } from "../chrome"
import Face from "../Face.vue"

interface Account {
  user_id: number
  name: string
  ok: boolean
  amount: string
  detail: string
  elapsed_ms: number
  at: string
}

interface Claim {
  place: string
  chat_id?: number
  link?: string
  at: string
  kind?: string
  wallet?: string
  got: number
  sum?: string
  accounts: Account[]
}

const claims = ref<Claim[]>([])
const today = ref("")
const loading = ref(true)
const error = ref("")

const clockFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
})

function partsOf(iso: string) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  const parts = clockFmt.formatToParts(date)
  const pick = (type: string) => parts.find((item) => item.type === type)?.value || ""
  return {
    ymd: `${pick("year")}-${pick("month")}-${pick("day")}`,
    month: pick("month"),
    day: pick("day"),
    clock: `${pick("hour")}:${pick("minute")}:${pick("second")}`,
  }
}

function dayLabel(iso: string) {
  const item = partsOf(iso)
  if (!item) return ""
  const today = partsOf(new Date().toISOString())
  if (today && item.ymd === today.ymd) return "今天"
  const [year, month, day] = (today?.ymd || item.ymd).split("-").map(Number)
  const yesterday = new Date(Date.UTC(year, month - 1, day - 1)).toISOString().slice(0, 10)
  if (item.ymd === yesterday) return "昨天"
  return `${Number(item.month)}月${Number(item.day)}日`
}

function clock(iso: string) {
  return partsOf(iso)?.clock || ""
}

const groups = computed(() => {
  const rows: { label: string; items: { claim: Claim; index: number }[] }[] = []
  claims.value.forEach((claim, index) => {
    const label = dayLabel(claim.at)
    const last = rows[rows.length - 1]
    if (!last || last.label !== label) rows.push({ label, items: [] })
    rows[rows.length - 1].items.push({ claim, index })
  })
  return rows
})

const REASONS: [string, string][] = [
  ["已领取过", "已领取过"],
  ["已領取過", "已领取过"],
  ["已被抢完", "已抢完"],
  ["已被领完", "已抢完"],
  ["已抢完", "已抢完"],
  ["已领完", "已抢完"],
  ["手慢", "已抢完"],
  ["抢光", "已抢完"],
  ["太火爆", "繁忙"],
  ["繁忙", "繁忙"],
  ["频繁", "繁忙"],
  ["flood", "繁忙"],
  ["窗口打不开", "窗口打不开"],
  ["网页验证没有打开", "网页没打开"],
  ["没有打开", "窗口打不开"],
  ["领取超时", "超时"],
  ["超时", "超时"],
  ["本期未下注", "本期未下注"],
  ["只有本期下注", "需本期下注"],
  ["专属", "专属红包"],
  ["专享", "专属红包"],
  ["数据校验", "校验失败"],
  ["其他成员", "不是你的验证"],
  ["不是你的验证", "不是你的验证"],
  ["不是给你的验证", "不是你的验证"],
  ["入群验证", "不是你的验证"],
  ["答案不在选项", "答案不对"],
  ["验证码错误", "验证码错误"],
  ["答案错误", "答案错误"],
  ["没有弹出验证码", "没有验证码"],
  ["算式无法计算", "算式算不出"],
  ["私信验证没有回执", "私信无回执"],
  ["口令没有回执", "口令无回执"],
  ["尚未注册", "未注册"],
  ["未注册", "未注册"],
  ["请先注册", "未注册"],
  ["需要等待", "需要等待"],
  ["发言次数不足", "发言不足"],
  ["发言不足", "发言不足"],
  ["用户不存在", "用户不存在"],
  ["封禁", "已封禁"],
  ["只有新用户", "限新用户"],
  ["premium", "限会员"],
  ["会员", "限会员"],
  ["当日充值", "需充值"],
  ["牌局数据不符合", "条件不符"],
  ["小程序", "需小程序"],
  ["账号被限制", "账号被限制"],
  ["没有回执", "没有回执"],
  ["已过期", "已过期"],
  ["已结束", "已结束"],
]

function reasonText(detail: string) {
  const raw = String(detail || "").replace(/\s+/g, " ").trim()
  if (!raw || raw === "✅") return ""
  const lower = raw.toLowerCase()
  const waited = raw.match(/需要等待\s*(\d+)\s*秒/)
  if (waited) return `等待 ${waited[1]} 秒`
  for (const [needle, label] of REASONS) {
    if (lower.includes(needle.toLowerCase())) return label
  }
  const cleaned = raw.replace(/^(节点|Telegram|主体)[:：]\s*/, "").replace(/^[^\u4e00-\u9fffA-Za-z0-9]+/, "").trim()
  if (!cleaned || /红包明细|恭喜发财|我是人类|领取情况/.test(cleaned)) return ""
  return cleaned.length > 40 ? cleaned.slice(0, 40) : cleaned
}

function moneyText(amount: string) {
  const raw = String(amount || "").trim()
  const unit = raw.match(/\b(USDT|USD|CNY|TRX|TON|GRAM|KKCOIN|WLCOIN|FTCOIN|JIBA|AIDU|DDCOIN|JJCOIN)\b/i)?.[1]?.toUpperCase() || ""
  const number = raw.match(/(\d+(?:\.\d+)?)/)?.[1]
  if (!number || !unit) return raw
  const name = unit === "USDT" || unit === "USD" ? "USDT（USD）" : unit === "GRAM" || unit === "TON" ? "Gram（TON）" : unit
  return `${number} ${name}`
}

function placeText(place: string) {
  return String(place || "").replace(/^@/, "") || "未知"
}

function whoText(item: Claim) {
  const names = item.accounts.map((account) => account.name || String(account.user_id)).filter(Boolean)
  const who = names.join("、") || "未知"
  return `${who} - ${placeText(item.place)}`
}

function resultText(account: Account) {
  if (account.ok) return moneyText(account.amount) || "领到"
  return reasonText(account.detail) || "没领到"
}

function walletText(wallet?: string) {
  const name = String(wallet || "").trim().replace(/^@+/, "")
  return name ? `@${name}` : ""
}

function groupText(item: Claim) {
  if (!item.accounts.some((account) => account.ok)) return "没领到"
  return item.sum || `领到 ${item.got} 人`
}

function rowText(item: Claim) {
  if (item.accounts.length > 1) return groupText(item)
  return resultText(item.accounts[0])
}

function rowOk(item: Claim) {
  return item.accounts.some((account) => account.ok)
}

function openMessage(url?: string) {
  if (!url) return
  haptic()
  const web = tg()
  if (web?.openTelegramLink) {
    web.openTelegramLink(url)
    return
  }
  window.open(url, "_blank", "noopener")
}

async function load() {
  loading.value = true
  error.value = ""
  try {
    const data = await api<{ claims: Claim[]; today?: string }>("/api/claims")
    claims.value = data.claims || []
    today.value = data.today || ""
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <main class="page claims dock-page">
    <t-navbar :fixed="false" title="领取记录" :safe-area-inset-top="false" />
    <p v-if="loading && !claims.length && !error" class="hint">正在加载</p>
    <div v-else-if="error && !claims.length" class="state">
      <t-empty :description="error" />
      <t-button theme="primary" @click="load">重试</t-button>
    </div>
    <template v-else>
      <p v-if="today" class="today">今天 {{ today }}</p>
      <t-empty v-if="!claims.length" description="还没有领取记录" />
      <t-cell-group v-for="group in groups" :key="group.label" :title="group.label">
        <template v-for="{ claim: item, index } in group.items" :key="`${item.at}-${item.place}-${index}`">
          <t-cell :arrow="Boolean(item.link)" :hover="Boolean(item.link)" @click="openMessage(item.link)">
            <template #left-icon>
              <Face
                :name="item.accounts.length === 1 ? (item.accounts[0].name || placeText(item.place)) : placeText(item.place)"
                :peer="item.accounts.length === 1 ? item.accounts[0].user_id : item.chat_id"
              />
            </template>
            <template #title>
              <b class="claim-who">{{ whoText(item) }}</b>
            </template>
            <template #description>
              <span class="claim-meta">
                <span v-if="walletText(item.wallet)">{{ walletText(item.wallet) }}</span>
                <span class="claim-result" :class="rowOk(item) ? 'ok' : 'bad'">{{ rowText(item) }}</span>
              </span>
            </template>
            <time class="claim-time">{{ clock(item.at) }}</time>
          </t-cell>
        </template>
      </t-cell-group>
    </template>
  </main>
</template>
