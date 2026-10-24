import { reactive } from "vue"
import { api, openHome, type Account, type Home, type Policy } from "./api"

export const home = reactive({
  loaded: false,
  loading: false,
  error: "",
  accounts: [] as Account[],
  policy: null as Policy | null,
  packets: [] as { id: string; title: string; kind: string }[],
  balance_at: "",
  pending: "",
  offline: [] as string[],
  join_open: false,
  notify: { bound: false, username: "" },
  message: "",
})

export function applyHome(data: Home, own = false) {
  if (own) epoch += 1
  home.accounts = data.accounts || []
  home.policy = data.policy
  home.packets = data.packets || []
  home.balance_at = data.balance_at || ""
  home.pending = data.pending || ""
  home.offline = data.offline || []
  home.join_open = !!data.join_open
  home.notify = {
    bound: !!data.notify?.bound,
    username: String(data.notify?.username || "").replace(/^@+/, ""),
  }
  home.message = data.message || ""
  home.loaded = true
  home.error = ""
}

let refreshTimer = 0
let flight: Promise<void> | null = null
let paint: ((ratio: number) => void) | null = null
let epoch = 0
let writeSerial = 0
let settledSerial = 0
let chain: Promise<void> = Promise.resolve()

function enqueue<T>(job: () => Promise<T>): Promise<T> {
  const run = chain.then(job, job)
  chain = run.then(() => {}, () => {})
  return run
}

export function loadHome(onProgress?: (ratio: number) => void) {
  if (onProgress) paint = onProgress
  if (flight) return flight
  flight = pull().finally(() => {
    flight = null
    paint = null
  })
  return flight
}

function absorb(seen: number, data: Home) {
  const dirty = writeSerial !== seen || settledSerial < writeSerial
  if (dirty && home.policy) {
    const flags = new Map(home.accounts.map((item) => [item.user_id, {
      paused: item.paused,
      join: item.join,
      watch: item.watch,
    }]))
    const accounts = (data.accounts || []).map((item) => {
      const prev = flags.get(item.user_id)
      return prev ? { ...item, ...prev } : item
    })
    applyHome({ ...data, policy: home.policy, accounts })
    return
  }
  applyHome(data)
}

async function pull() {
  const seen = writeSerial
  home.loading = !home.loaded
  const paintNow = paint
  try {
    if (paintNow) await openHome((ratio) => paint?.(ratio), (data) => absorb(seen, data))
    else absorb(seen, await api<Home>("/api/home", {}, true))
    home.error = ""
  } catch (err) {
    if (writeSerial !== seen) return
    if (!home.loaded) home.error = err instanceof Error ? err.message : "处理失败"
  } finally {
    home.loading = false
    if (refreshTimer) window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => {
      refreshTimer = 0
      if (home.loaded) loadHome()
    }, 15000)
  }
}

export function noteWrite() {
  epoch += 1
}

export function commitHome(job: () => Promise<Home>) {
  const serial = ++writeSerial
  noteWrite()
  return enqueue(async () => {
    try {
      const data = await job()
      if (serial === writeSerial) {
        applyHome(data)
        settledSerial = serial
      }
      return data
    } catch (err) {
      if (serial === writeSerial) settledSerial = serial
      throw err
    }
  })
}

export function takePolicy(path: string, opts: RequestInit = {}, quiet = false) {
  const serial = ++writeSerial
  noteWrite()
  return enqueue(async () => {
    try {
      const data = await api<{ policy: Policy; offline?: string[]; message?: string }>(path, opts, quiet)
      if (serial === writeSerial) {
        if (data.policy) home.policy = data.policy
        if (data.offline) home.offline = data.offline
        settledSerial = serial
      }
      return data
    } catch (err) {
      if (serial === writeSerial) settledSerial = serial
      throw err
    }
  })
}

export function patch(body: Record<string, unknown>) {
  return takePolicy("/api/policy", { method: "PATCH", body: JSON.stringify(body) }, true)
}
