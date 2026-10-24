import { holdLiquid } from "./liquid"

export class ApiError extends Error {}

export interface Entry {
  id?: string
  title?: string
  username?: string
  chat_id?: number
  user_id?: number
  kind?: string
  active_at?: string
  off?: number[]
  sign?: boolean
  speak?: boolean
}

export function windowKey(item: { kind?: string; chat_id?: number; user_id?: number }) {
  if (item.kind === "bot") return `bot:${item.user_id || item.chat_id || ""}`
  if (item.kind === "user") return `user:${item.user_id || item.chat_id || ""}`
  return `chat:${item.chat_id || ""}`
}

export function orderWindows<T extends { kind?: string; chat_id?: number; user_id?: number; title?: string }>(
  rows: T[],
  rank: Record<string, string>,
) {
  return [...rows].sort((a, b) => {
    const left = rank[windowKey(a)] || ""
    const right = rank[windowKey(b)] || ""
    if (left !== right) {
      if (!left) return 1
      if (!right) return -1
      return right < left ? -1 : 1
    }
    return String(a.title || "").localeCompare(String(b.title || ""), "zh")
  })
}

export const PEER_TABS = [
  ["all", "全部"],
  ["user", "用户"],
  ["group", "群组"],
  ["channel", "频道"],
  ["bot", "机器人"],
] as const

export function peerKind(kind?: string) {
  if (kind === "user") return "用户"
  if (kind === "channel") return "频道"
  if (kind === "bot") return "机器人"
  return "群组"
}

export function peerBucket(kind?: string) {
  if (kind === "user" || kind === "channel" || kind === "bot") return kind
  return "group"
}

export function peerLine(item: { kind?: string; username?: string; chat_id?: number; user_id?: number }) {
  const label = peerKind(item.kind)
  const name = String(item.username || "").replace(/^@/, "")
  if (name) return `${label} · @${name}`
  const id = item.kind === "bot" || item.kind === "user" ? (item.user_id || item.chat_id) : item.chat_id
  return id ? `${label} · ${id}` : label
}

export function peerEmpty(tab: string) {
  if (tab === "user") return "没有用户"
  if (tab === "group") return "没有群组"
  if (tab === "channel") return "没有频道"
  if (tab === "bot") return "没有机器人"
  return "没有窗口"
}

export interface Policy {
  modes: Record<string, boolean>
  mode_delays: Record<string, { low: number; high: number }>
  window_mode: string
  window_lists?: Record<string, Entry[]>
  windows: Entry[]
  bot_mode: string
  bots: Entry[]
  burn: boolean
  burns: Record<string, boolean>
  delays: { wallet: string; low: number; high: number }[]
  hot_on: boolean
  hot_windows: Entry[]
  hot_lines: string[]
  hot_low: number
  hot_high: number
  hot_source: string
  hot_packs: string[]
  hot_drop: number
  hot_open: string
  hot_close: string
  hot_cap: number
  hot_off: number[]
  open_at: string
  close_at: string
  coins: Record<string, boolean>
  min_counts: Record<string, string>
  min_totals: Record<string, string>
  min_eaches: Record<string, string>
  unknown_coin: boolean
  count_over: boolean
  auto_receive: boolean
  window_block_keywords: string[]
  window_block_minutes: number
  web_pay: { fll: boolean; cf: boolean }
  pay_ready?: { fll: boolean; cf: boolean }
  pay_admin?: boolean
  phrase_say: { low: number; high: number }
  hide_zero: boolean
  show_min: Record<string, string>
  paused: number[]
}

export interface Account {
  user_id: number
  node_id: string
  name: string
  username: string
  online: boolean
  reason: string
  paused: boolean
  join: boolean
  watch: boolean
  brief?: {
    state: string
  }
  dc: number
  home: string
  city: string
  expected_city: string
  added_at: string
  active_at: string
  active: string
  nft: boolean
  org: string
  premium: boolean
  phone?: string
  about?: string
  birthday?: string
  lang?: string
  verified?: boolean
  restricted?: boolean
  scam?: boolean
  fake?: boolean
  usdt: string
  wallets: Record<string, Record<string, string>>
  errors: string[]
}

export interface PacketModule {
  id: string
  title: string
  kind: string
}

export interface Home {
  accounts: Account[]
  policy: Policy
  packets: PacketModule[]
  balance_at: string
  pending: string
  offline: string[]
  join_open?: boolean
  notify?: { bound?: boolean; username?: string }
  message?: string
}

let token = ""

function webapp() {
  return window.Telegram?.WebApp
}

async function readInit() {
  const web = webapp()
  try { web?.ready() } catch { /* 旧客户端没有 ready */ }
  for (let i = 0; i < 20; i += 1) {
    const init = web?.initData || ""
    if (init) return init
    await new Promise((resolve) => window.setTimeout(resolve, 50))
  }
  return ""
}

async function session() {
  const init = await readInit()
  if (!init) throw new ApiError("请从 Telegram 打开")
  const res = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ init_data: init }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok || !data.ok || !data.token) throw new ApiError(data.message || "Telegram：身份无效")
  token = data.token
}

export async function openHome(onProgress: (ratio: number) => void, onHome?: (home: Home) => void): Promise<Home> {
  onProgress(0)
  let sessionWeight = 0
  if (!token) {
    await session()
    sessionWeight = 1
  }
  const pull = () => fetch("/api/home?progress=1", { headers: { Authorization: `Bearer ${token}` } })
  let res = await pull()
  if (res.status === 401) {
    token = ""
    await session()
    sessionWeight = 1
    res = await pull()
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new ApiError(data.message || "处理失败")
  }
  const kind = res.headers.get("content-type") || ""
  if (!kind.includes("ndjson")) {
    const data = await res.json().catch(() => ({}))
    if (data.ok === false) throw new ApiError(data.message || "处理失败")
    onHome?.(data as Home)
    onProgress(1)
    return data as Home
  }
  if (!res.body) throw new ApiError("处理失败")
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ""
  let nodeDone = 0
  let nodeTotal = 0
  let home: Home | null = null
  let opened = false
  let fail: (err: Error) => void = () => {}
  let succeed: (data: Home) => void = () => {}
  const ready = new Promise<Home>((resolve, reject) => {
    succeed = resolve
    fail = reject
  })
  const report = () => {
    if (!nodeTotal) return
    onProgress((sessionWeight + nodeDone) / (sessionWeight + nodeTotal))
  }
  const take = (line: string) => {
    if (!line || line[0] !== "{") return
    let row: { done?: number; total?: number; home?: Home; ok?: boolean; message?: string }
    try {
      row = JSON.parse(line)
    } catch {
      throw new ApiError("处理失败")
    }
    if (row.ok === false) throw new ApiError(row.message || "处理失败")
    if (typeof row.total === "number" && row.total > 0) nodeTotal = row.total
    if (typeof row.done === "number") nodeDone = row.done
    if (row.home) {
      home = row.home
      onHome?.(row.home)
      if (!opened) {
        opened = true
        succeed(row.home)
      }
    }
    report()
  }
  const reading = (async () => {
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let cut = buf.indexOf("\n")
        while (cut >= 0) {
          take(buf.slice(0, cut).trim())
          buf = buf.slice(cut + 1)
          cut = buf.indexOf("\n")
        }
      }
      const rest = buf.trim()
      if (rest) take(rest)
      if (!home) throw new ApiError("处理失败")
      onProgress(1)
    } catch (err) {
      if (!opened) fail(err instanceof Error ? err : new ApiError("处理失败"))
    }
  })()
  void reading
  return ready
}

export async function api<T>(path: string, opts: RequestInit = {}, quiet = false): Promise<T> {
  const release = quiet ? () => {} : holdLiquid()
  try {
    if (!token) await session()
    const run = async () => {
      const headers = new Headers(opts.headers)
      headers.set("Authorization", `Bearer ${token}`)
      if (opts.body && !(opts.body instanceof FormData) && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json")
      }
      return fetch(path, { ...opts, headers })
    }
    let res = await run()
    if (res.status === 401) {
      token = ""
      await session()
      res = await run()
    }
    const data = await res.json().catch(() => ({ ok: false, message: "处理失败" }))
    if (!res.ok || data.ok === false) throw new ApiError(data.message || "处理失败")
    return data as T
  } finally {
    release()
  }
}

export function hasTelegram() {
  return Boolean(webapp()?.initData)
}

const FACE_MS = 3_600_000
const faceHit = new Map<string, { url: string | null; tag: string; at: number }>()
const faceJob = new Map<string, Promise<string | null>>()
let faceBusy = 0
const faceQueue: Array<() => void> = []

function takeFace() {
  if (faceBusy < 3) {
    faceBusy += 1
    return Promise.resolve()
  }
  return new Promise<void>((resolve) => faceQueue.push(resolve))
}

function freeFace() {
  faceBusy -= 1
  const next = faceQueue.shift()
  if (!next) return
  faceBusy += 1
  next()
}

export function loadFace(peer?: number | null) {
  const id = Number(peer)
  if (!id) return Promise.resolve(null)
  const key = String(id)
  const hit = faceHit.get(key)
  if (hit && Date.now() - hit.at < FACE_MS) return Promise.resolve(hit.url)
  const pending = faceJob.get(key)
  if (pending) return pending
  const job = (async () => {
    await takeFace()
    try {
      if (!token) await session()
      const pull = () => {
        const headers: Record<string, string> = { Authorization: `Bearer ${token}` }
        if (hit?.tag) headers["If-None-Match"] = hit.tag
        return fetch(`/api/faces/${id}`, { headers, cache: "no-store" })
      }
      let res = await pull()
      if (res.status === 401) {
        token = ""
        await session()
        res = await pull()
      }
      if (res.status === 304 && hit) {
        const next = { ...hit, at: Date.now() }
        faceHit.set(key, next)
        return next.url
      }
      if (res.status === 204) {
        if (hit?.url) URL.revokeObjectURL(hit.url)
        faceHit.set(key, { url: null, tag: "", at: Date.now() })
        return null
      }
      if (res.status !== 200) return hit?.url ?? null
      const tag = res.headers.get("ETag") || ""
      if (hit?.url) URL.revokeObjectURL(hit.url)
      const url = URL.createObjectURL(await res.blob())
      faceHit.set(key, { url, tag, at: Date.now() })
      return url
    } catch {
      return hit?.url ?? null
    } finally {
      faceJob.delete(key)
      freeFace()
    }
  })()
  faceJob.set(key, job)
  return job
}

function delayText(value = 0) {
  if (value > 0 && value < 1) return `${Math.round(value * 1000)}ms`
  return `${value} 秒`
}

export function spanText(low = 0, high = 0) {
  if (low === high) return delayText(low)
  return `${delayText(low).replace(/ 秒$/, "")}–${delayText(high)}`
}

export function clockText(value: string) {
  const matched = String(value || "").trim().match(/^(\d{1,2}):(\d{2})/)
  if (!matched) return ""
  const hour = Number(matched[1])
  const minute = Number(matched[2])
  if (hour > 23 || minute > 59) return ""
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`
}

export function parseSpan(text: string) {
  let raw = text.trim().toLowerCase().replace(/\s+/g, "")
  const fromMs = raw.includes("毫秒") || raw.includes("ms")
  raw = raw.replace(/毫秒|ms|秒|s/g, "")
  const range = raw.match(/^(\d+)[-~～—到至](\d+)$/)
  let low = 0
  let high = 0
  if (range) {
    low = Number(range[1])
    high = Number(range[2])
  } else if (/^\d+$/.test(raw)) {
    low = Number(raw)
    high = low
  } else {
    return null
  }
  if (fromMs) {
    low = Math.round(low / 1000)
    high = Math.round(high / 1000)
  }
  return { low, high }
}

export const MODULES: [string, string][] = [
  ["phrase", "口令红包"],
  ["captcha", "算式计算"],
  ["webapp", "网页验证码"],
  ["direct", "按钮关键词"],
]

export const GRAB = MODULES

export function grabbing(item: { paused?: boolean }) {
  return !item.paused
}

export function joining(item: { join?: boolean }) {
  return !!item.join
}

export function watching(item: { watch?: boolean }) {
  return !!item.watch
}

export function switchHint(body: Record<string, unknown>) {
  const on = "grab" in body ? body.grab : "watch" in body ? body.watch : body.join
  return on ? "已打开" : "已关闭"
}

export const COINS = ["USDT", "CNY", "TRX", "TON", "KKCOIN", "WLCOIN", "FTCOIN", "JIBA", "AIDU", "DDCOIN", "JJCOIN"]

export function coinLabel(code: string) {
  if (code === "USDT") return "USDT（USD）"
  if (code === "TON") return "Gram（TON）"
  return code
}

export const AXIS = [
  ["off", "关闭"],
  ["white", "白名单"],
  ["black", "黑名单"],
] as const
