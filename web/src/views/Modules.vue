<script setup lang="ts">
import { useRouter } from "vue-router"
import { ref, watch } from "vue"
import { api, GRAB, spanText } from "../api"
import { haptic, tg } from "../chrome"
import { home, loadHome, patch } from "../state"

const router = useRouter()
const error = ref("")
const sheet = ref(false)
const busy = ref(false)
const draft = ref("")
const editing = ref<"fll" | "cf" | "">("")
const paySite = {
  fll: "https://2captcha.com",
  cf: "https://capmonster.cloud",
}
const payHost = {
  fll: "2captcha.com",
  cf: "capmonster.cloud",
}
const payAmount = ref({ fll: "", cf: "" })
const payState = ref({ fll: "", cf: "" })
if (!home.loaded) loadHome()

function canPay(key: "fll" | "cf") {
  return !!home.policy?.pay_admin || !!home.policy?.pay_ready?.[key]
}

function payText(key: "fll" | "cf") {
  if (!canPay(key)) return ""
  const state = payState.value[key]
  const amount = payAmount.value[key]
  if (state === "ok" && amount) return `余额 ${amount}`
  if (!home.policy?.pay_admin && state === "invalid") return "密钥无效"
  if (!state) return "正在查询余额"
  return "余额暂时查不到"
}

function openSite(event: MouseEvent, url: string) {
  event.preventDefault()
  haptic()
  const web = tg()
  if (web?.openLink) {
    web.openLink(url)
    return
  }
  window.open(url, "_blank", "noopener")
}

async function loadPay() {
  try {
    const data = await api<{ fll?: string; cf?: string; fll_state?: string; cf_state?: string }>("/api/pay-balance", {}, true)
    payAmount.value = { fll: data.fll || "", cf: data.cf || "" }
    payState.value = { fll: data.fll_state || "down", cf: data.cf_state || "down" }
  } catch {
    payState.value = { fll: "down", cf: "down" }
  }
}

watch(() => home.loaded, (ok) => { if (ok) void loadPay() }, { immediate: true })

watch(sheet, (open) => {
  if (!open) error.value = ""
})

function openPay(key: "fll" | "cf") {
  if (!home.policy || home.policy.pay_admin) return
  editing.value = key
  draft.value = ""
  error.value = ""
  sheet.value = true
}

async function pay(key: "fll" | "cf") {
  if (!home.policy || !canPay(key)) return
  haptic()
  const prev = home.policy.web_pay?.[key] !== false
  const next = !prev
  home.policy.web_pay = { fll: home.policy.web_pay?.fll !== false, cf: home.policy.web_pay?.cf !== false, [key]: next }
  error.value = ""
  try {
    await patch({ web_pay: { [key]: next } })
  } catch (err) {
    home.policy.web_pay = { ...home.policy.web_pay, [key]: prev }
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

async function sendPay(value: string) {
  const key = editing.value
  if (!key || busy.value) return
  haptic()
  error.value = ""
  busy.value = true
  try {
    await patch({ pay_keys: { [key]: value } })
    draft.value = ""
    sheet.value = false
    payState.value = { ...payState.value, [key]: "" }
    void loadPay()
  } catch (err) {
    error.value = err instanceof Error ? err.message : "处理失败"
  } finally {
    busy.value = false
  }
}

async function savePay() {
  const value = draft.value.trim()
  if (!value) {
    error.value = "请填写 token"
    return
  }
  await sendPay(value)
}

async function toggle(kind: string) {
  if (!home.policy) return
  haptic()
  const modes = { ...home.policy.modes, [kind]: !home.policy.modes[kind] }
  home.policy.modes[kind] = modes[kind]
  error.value = ""
  try {
    await patch({ modes })
  } catch (err) {
    home.policy.modes[kind] = !modes[kind]
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

async function receive() {
  if (!home.policy) return
  haptic()
  const next = home.policy.auto_receive === false
  home.policy.auto_receive = next
  error.value = ""
  try {
    await patch({ auto_receive: next })
  } catch (err) {
    home.policy.auto_receive = !next
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

function hoursText() {
  const open = home.policy?.open_at || "00:00"
  const close = home.policy?.close_at || "00:00"
  if (open === close) return "全天"
  return `${open}–${close}`
}

function modeLabel(kind: string) {
  const span = home.policy?.mode_delays?.[kind]
  const parts: string[] = []
  if (span && span.high > 0) parts.push(`延迟 ${spanText(span.low, span.high)}`)
  if (kind === "phrase" && home.policy?.burns?.phrase) parts.push("消息自毁")
  return parts.join(" · ")
}
</script>

<template>
  <section>
    <t-notice-bar v-if="error && !sheet" theme="error" :content="error" />
    <t-cell-group v-if="home.policy" title="运作">
      <t-cell title="运作时间" :description="hoursText()" arrow hover @click="router.push('/prefs/hours')" />
    </t-cell-group>
    <t-cell-group v-if="home.policy" title="领取">
      <t-cell
        v-for="[kind, label] in GRAB"
        :key="kind"
        :title="label"
        :description="modeLabel(kind)"
        arrow
        hover
        @click="router.push(`/modules/${kind}`)"
      >
        <div @click.stop>
          <t-switch :model-value="!!home.policy.modes[kind]" @change="toggle(kind)" />
        </div>
      </t-cell>
    </t-cell-group>
    <t-cell-group v-if="home.policy" title="收款">
      <t-cell title="OKPay 自动收款">
        <t-switch :model-value="home.policy.auto_receive !== false" @change="receive" />
      </t-cell>
    </t-cell-group>
    <t-cell-group v-if="home.policy" title="打码">
      <t-cell title="hCaptcha（法拉利）" :description="payText('fll') || undefined">
        <t-button v-if="!canPay('fll')" size="small" theme="primary" @click="openPay('fll')">填写</t-button>
        <div v-else class="pay-ready" @click.stop>
          <t-button v-if="!home.policy.pay_admin" size="small" variant="outline" @click="openPay('fll')">更换</t-button>
          <t-switch :model-value="home.policy.web_pay?.fll !== false" :disabled="!home.policy.modes?.webapp" @change="pay('fll')" />
        </div>
      </t-cell>
      <t-cell title="Cloudflare（彩金）" :description="payText('cf') || undefined">
        <t-button v-if="!canPay('cf')" size="small" theme="primary" @click="openPay('cf')">填写</t-button>
        <div v-else class="pay-ready" @click.stop>
          <t-button v-if="!home.policy.pay_admin" size="small" variant="outline" @click="openPay('cf')">更换</t-button>
          <t-switch :model-value="home.policy.web_pay?.cf !== false" :disabled="!home.policy.modes?.webapp" @change="pay('cf')" />
        </div>
      </t-cell>
    </t-cell-group>
    <t-popup v-model="sheet" placement="bottom" :close-btn="true">
      <div class="sheet">
        <h2 class="sheet-title">{{ editing === "cf" ? "Cloudflare（彩金）" : "hCaptcha（法拉利）" }}</h2>
        <p v-if="editing === 'fll' || editing === 'cf'" class="hint">
          打开
          <a :href="paySite[editing]" @click="openSite($event, paySite[editing])">{{ payHost[editing] }}</a>
          ，复制 token 贴到这里。费用从你的账户扣。填过的可以直接换。
        </p>
        <t-notice-bar v-if="error" theme="error" :content="error" />
        <label class="api-field">
          <span>token</span>
          <t-input
            v-model="draft"
            type="password"
            :placeholder="editing && home.policy?.pay_ready?.[editing] ? '粘贴新的 token' : '粘贴到这里'"
            borderless
          />
        </label>
        <div class="sheet-actions">
          <t-button theme="primary" :loading="busy" @click="savePay">
            {{ editing && home.policy?.pay_ready?.[editing] ? "替换" : "保存" }}
          </t-button>
          <t-button
            v-if="editing && home.policy?.pay_ready?.[editing]"
            theme="danger"
            variant="outline"
            :disabled="busy"
            @click="sendPay('')"
          >
            清除
          </t-button>
        </div>
      </div>
    </t-popup>
  </section>
</template>

<style scoped>
.pay-ready {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
