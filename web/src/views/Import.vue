<script setup lang="ts">
import { computed, ref, watch } from "vue"
import { api, type Home } from "../api"
import { haptic } from "../chrome"
import { applyHome } from "../state"

const open = defineModel<boolean>("show", { default: false })

type Phase = "pick" | "phone" | "sending" | "code" | "signing" | "password" | "confirming" | "qr" | "waiting" | "packing" | "packed" | "done"

type PackRow = { label: string; ok: boolean; fresh?: boolean; message?: string }

const phase = ref<Phase>("pick")
const phone = ref("")
const code = ref("")
const password = ref("")
const image = ref("")
const error = ref("")
const note = ref("")
const packed = ref<PackRow[]>([])
const packFile = ref<HTMLInputElement | null>(null)
const draftKey = "meiguan-add"
let epoch = 0
let pollToken = 0
let pollCtrl: AbortController | null = null
let polling = false

loadDraft()

watch([phase, phone, code, image], saveDraft)

const active = computed(() => (phase.value === "done" || phase.value === "packed" ? 1 : 0))

const loadingText = computed(() => {
  if (phase.value === "sending") return "正在发送验证码"
  if (phase.value === "signing" || phase.value === "confirming") return "正在登录"
  if (phase.value === "qr") return "正在生成二维码"
  if (phase.value === "packing") return "正在添加"
  return ""
})

function alive(mark: number) {
  return mark === epoch
}

function loadDraft() {
  try {
    const raw = sessionStorage.getItem(draftKey)
    if (!raw) return
    const data = JSON.parse(raw) as { phase?: Phase; phone?: string; code?: string; image?: string }
    if (data.phase !== "phone" && data.phase !== "code" && data.phase !== "password" && data.phase !== "waiting") return
    phase.value = data.phase
    phone.value = data.phone || ""
    code.value = data.code || ""
    image.value = data.image || ""
  } catch {
    sessionStorage.removeItem(draftKey)
  }
}

function saveDraft() {
  if (phase.value === "pick" || phase.value === "done" || phase.value === "packed") {
    sessionStorage.removeItem(draftKey)
    return
  }
  const keep = phase.value === "sending" ? "phone"
    : phase.value === "signing" ? "code"
    : phase.value === "confirming" ? "password"
    : phase.value === "qr" ? "waiting"
    : phase.value
  try {
    sessionStorage.setItem(draftKey, JSON.stringify({
      phase: keep,
      phone: phone.value,
      code: code.value,
      image: keep === "waiting" ? image.value : "",
    }))
  } catch { /* 浏览器不允许时，当前页仍保留这一步 */ }
}

function wipe() {
  epoch += 1
  pollCtrl?.abort()
  polling = false
  phase.value = "pick"
  phone.value = ""
  code.value = ""
  password.value = ""
  image.value = ""
  error.value = ""
  note.value = ""
  packed.value = []
  sessionStorage.removeItem(draftKey)
}

function resumePoll() {
  if (polling) return
  const mark = epoch
  const token = ++pollToken
  const ctrl = new AbortController()
  pollCtrl = ctrl
  poll(mark, token, ctrl)
}

watch(open, (value) => {
  if (!value) {
    if (phase.value === "done" || phase.value === "packed") wipe()
    return
  }
  if (phase.value === "pick" || phase.value === "done") return
  if (phase.value === "waiting" || phase.value === "qr") {
    if (image.value) {
      phase.value = "waiting"
      resumePoll()
    } else if (phase.value === "waiting") startQr()
    return
  }
  syncStep()
})

async function syncStep() {
  const mark = epoch
  try {
    const data = await api<Home & { step?: string; phone?: string; image?: string }>("/api/login")
    if (!alive(mark)) return
    if (data.accounts) {
      finish(data)
      return
    }
    if (data.phone) phone.value = data.phone
    if (data.image) image.value = data.image
    if (data.step === "password") phase.value = "password"
    else if (data.step === "code") phase.value = "code"
    else if (data.step === "qr") {
      phase.value = "waiting"
      resumePoll()
    } else if (!data.step && (phase.value === "code" || phase.value === "password")) {
      error.value = "请重新发送验证码"
      phase.value = "phone"
    }
  } catch (err) {
    if (!alive(mark) || stopped(err)) return
  }
}

function finishClose() {
  wipe()
  open.value = false
}

function fail(err: unknown, back: Phase) {
  error.value = err instanceof Error ? err.message : "添加失败"
  phase.value = back
}

function finish(data: Home) {
  applyHome(data, true)
  note.value = data.message || ""
  phase.value = "done"
  haptic()
}

async function sendPhone() {
  if (phase.value !== "phone" || !phone.value.trim()) return
  const mark = epoch
  error.value = ""
  phase.value = "sending"
  haptic()
  try {
    const data = await api<{ step: string; phone: string }>("/api/login/phone", {
      method: "POST",
      body: JSON.stringify({ phone: phone.value }),
    })
    if (!alive(mark)) {
      await api("/api/login/cancel", { method: "POST", body: "{}" }).catch(() => {})
      return
    }
    phone.value = data.phone || phone.value
    phase.value = "code"
  } catch (err) {
    if (!alive(mark)) return
    fail(err, "phone")
  }
}

async function sendCode() {
  if (phase.value !== "code" || !code.value.trim()) return
  const mark = epoch
  error.value = ""
  phase.value = "signing"
  haptic()
  try {
    const data = await api<Home & { step?: string }>("/api/login/code", {
      method: "POST",
      body: JSON.stringify({ code: code.value }),
    })
    if (!alive(mark)) {
      await api("/api/login/cancel", { method: "POST", body: "{}" }).catch(() => {})
      return
    }
    if (data.step === "password") {
      phase.value = "password"
      return
    }
    finish(data)
  } catch (err) {
    if (!alive(mark)) return
    fail(err, "code")
  }
}

async function sendPassword() {
  if (phase.value !== "password" || !password.value.trim()) return
  const mark = epoch
  error.value = ""
  phase.value = "confirming"
  haptic()
  try {
    const data = await api<Home & { step?: string }>("/api/login/password", {
      method: "POST",
      body: JSON.stringify({ password: password.value }),
    })
    if (!alive(mark)) return
    finish(data)
  } catch (err) {
    if (!alive(mark)) return
    fail(err, "password")
  }
}

function stopped(err: unknown) {
  return err instanceof DOMException && err.name === "AbortError"
}

async function startQr() {
  const mark = epoch
  const token = ++pollToken
  pollCtrl?.abort()
  const ctrl = new AbortController()
  pollCtrl = ctrl
  error.value = ""
  image.value = ""
  phase.value = "qr"
  haptic()
  try {
    const data = await api<{ image?: string }>("/api/login/qr", {
      method: "POST",
      body: "{}",
      signal: ctrl.signal,
    })
    if (!alive(mark) || token !== pollToken) return
    image.value = data.image || ""
    phase.value = "waiting"
    poll(mark, token, ctrl)
  } catch (err) {
    if (!alive(mark) || token !== pollToken || stopped(err)) return
    fail(err, "pick")
  }
}

async function poll(mark: number, token: number, ctrl: AbortController) {
  polling = true
  try {
  while (alive(mark) && token === pollToken && phase.value === "waiting") {
    try {
      const data = await api<Home & { step?: string; image?: string }>("/api/login", { signal: ctrl.signal })
      if (!alive(mark) || token !== pollToken) return
      if (data.image) image.value = data.image
      if (data.step === "password") {
        phase.value = "password"
        return
      }
      if (data.accounts) {
        finish(data)
        return
      }
      if (data.step !== "qr") {
        error.value = "设备扫码中断"
        return
      }
    } catch (err) {
      if (!alive(mark) || token !== pollToken || stopped(err)) return
      error.value = err instanceof Error ? err.message : "设备扫码中断"
      return
    }
  }
  } finally {
    if (token === pollToken) polling = false
  }
}

async function stepBack() {
  haptic()
  error.value = ""
  if (phase.value === "password") {
    phase.value = "code"
    return
  }
  if (phase.value !== "code" && phase.value !== "phone") return
  const back = phase.value === "code" ? "phone" : "pick"
  if (back === "phone") code.value = ""
  phase.value = back
  await api("/api/login/cancel", { method: "POST", body: "{}" }).catch(() => {})
}

function chooseCode() {
  error.value = ""
  phase.value = "phone"
  haptic()
}

function begin(kind: "code" | "qr" | "pack") {
  error.value = ""
  if (kind === "pack") {
    choosePack()
    return
  }
  if (kind === "code") {
    phase.value = "phone"
    open.value = true
    haptic()
    return
  }
  open.value = true
  startQr()
}

defineExpose({ begin })

function choosePack() {
  error.value = ""
  if (packFile.value) packFile.value.value = ""
  packFile.value?.click()
  haptic()
}

function packLine(row: PackRow) {
  if (!row.ok) return row.message || "添加失败"
  return row.fresh ? "已添加" : "已在账号里"
}

async function onPack(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || phase.value === "packing") return
  if (file.size > 8_000_000) {
    error.value = "压缩包太大"
    return
  }
  const mark = epoch
  error.value = ""
  phase.value = "packing"
  open.value = true
  haptic()
  const body = new FormData()
  body.append("file", file, file.name)
  try {
    const data = await api<Home & { results?: PackRow[]; added?: number }>("/api/login/pack", {
      method: "POST",
      body,
    })
    if (!alive(mark)) return
    applyHome(data, true)
    packed.value = data.results || []
    const added = data.added || 0
    const failed = packed.value.filter((row) => !row.ok).length
    if (added && failed) note.value = `已添加 ${added} 个，失败 ${failed} 个`
    else if (added) note.value = `已添加 ${added} 个`
    else if (failed) note.value = "没有添加成功"
    else note.value = "没有新的账号"
    phase.value = "packed"
    haptic()
  } catch (err) {
    if (!alive(mark)) return
    fail(err, "pick")
  }
}
</script>

<template>
  <input ref="packFile" class="pack-file" type="file" accept=".zip,application/zip" @change="onPack" />
  <t-popup v-model="open" placement="bottom" :close-btn="true">
    <div class="sheet">
      <h2 class="sheet-title">添加账号</h2>
      <t-steps v-if="phase !== 'pick'" class="steps" :current="active" theme="dot">
        <t-step-item title="登录" />
        <t-step-item title="完成" />
      </t-steps>
      <t-notice-bar v-if="error" theme="error" :content="error" />

      <t-cell-group v-if="phase === 'pick'">
        <t-cell title="验证码" description="向手机号发送验证码" arrow hover @click="chooseCode" />
        <t-cell title="二维码" description="在 Telegram 的设备里扫码" arrow hover @click="startQr" />
        <t-cell title="压缩包" description="仅支持 .zip" arrow hover @click="choosePack" />
      </t-cell-group>

      <p v-else-if="loadingText" class="hint">{{ loadingText }}</p>

      <form v-else-if="phase === 'phone'" @submit.prevent="sendPhone">
        <p class="hint">填写带区号的手机号。</p>
        <t-cell-group>
          <t-input v-model="phone" type="tel" label="手机号" placeholder="+88812345678" borderless />
        </t-cell-group>
        <div class="sheet-bar row">
          <t-button variant="outline" type="button" @click="stepBack">上一步</t-button>
          <t-button theme="primary" type="submit" :disabled="!phone.trim()">下一步</t-button>
        </div>
      </form>

      <form v-else-if="phase === 'code'" @submit.prevent="sendCode">
        <p class="hint">验证码已发送至 {{ phone }}</p>
        <t-cell-group>
          <t-input v-model="code" label="验证码" placeholder="验证码" borderless />
        </t-cell-group>
        <div class="sheet-bar row">
          <t-button variant="outline" type="button" @click="stepBack">上一步</t-button>
          <t-button theme="primary" type="submit" :disabled="!code.trim()">下一步</t-button>
        </div>
      </form>

      <form v-else-if="phase === 'password'" @submit.prevent="sendPassword">
        <p class="hint">这个号开了二级密码。</p>
        <t-cell-group>
          <t-input v-model="password" type="password" label="二级密码" placeholder="二级密码" borderless />
        </t-cell-group>
        <div class="sheet-bar row">
          <t-button variant="outline" type="button" @click="stepBack">上一步</t-button>
          <t-button theme="primary" type="submit" :disabled="!password.trim()">下一步</t-button>
        </div>
      </form>

      <template v-else-if="phase === 'waiting'">
        <p class="hint">打开要登录的 Telegram：设置 - 设备 - 链接桌面设备。扫完后会自动登录。</p>
        <img v-if="image" class="qr" :src="`data:image/png;base64,${image}`" alt="登录二维码" />
        <p v-if="!error" class="hint">等待设备扫码</p>
        <div v-if="error" class="sheet-bar">
          <t-button block variant="outline" @click="startQr">重新生成</t-button>
        </div>
      </template>

      <template v-else-if="phase === 'packed'">
        <p class="hint">{{ note }}</p>
        <div class="pack-list">
          <p v-for="(row, index) in packed" :key="index" class="pack-row">
            <span class="pack-name">{{ row.label }}</span>
            <span :class="row.ok ? 'pack-ok' : 'pack-bad'">{{ packLine(row) }}</span>
          </p>
        </div>
        <div class="sheet-bar">
          <t-button theme="primary" block @click="finishClose">完成</t-button>
        </div>
      </template>

      <template v-else-if="phase === 'done'">
        <t-result theme="success" title="已添加" :description="note" />
        <div class="sheet-bar">
          <t-button theme="primary" block @click="finishClose">完成</t-button>
        </div>
      </template>
    </div>
  </t-popup>
</template>
