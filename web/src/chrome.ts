let backFn: (() => void) | null = null
let mainFn: (() => void) | null = null
let backBound = false
let mainBound = false

export function tg() {
  return window.Telegram?.WebApp
}

export function openedInTelegram() {
  const web = tg()
  if (!web) return false
  if (web.initData) return true
  return Boolean(web.platform && web.platform !== "unknown")
}

function applyInset() {
  const web = tg()
  const content = web?.contentSafeAreaInset || {}
  const safe = web?.safeAreaInset || {}
  const top = (content.top || 0) + (safe.top || 0)
  const bottom = Math.max(content.bottom || 0, safe.bottom || 0)
  const height = web?.viewportStableHeight || window.innerHeight
  const root = document.documentElement.style
  root.setProperty("--safe-top", `${top}px`)
  root.setProperty("--safe-bottom", `${bottom}px`)
  root.setProperty("--app-height", `${height}px`)
}

export function bootChrome() {
  const web = tg()
  if (!web) return
  web.ready()
  web.expand()
  try { web.disableVerticalSwipes?.() } catch { /* 旧客户端没有这个方法 */ }
  try { web.setHeaderColor("#ffffff") } catch { /* 旧客户端没有这个方法 */ }
  try { web.setBackgroundColor("#ffffff") } catch { /* 同上 */ }
  try { web.setBottomBarColor?.("#ffffff") } catch { /* 同上 */ }
  web.onEvent("viewportChanged", applyInset)
  web.onEvent("safeAreaChanged", applyInset)
  web.onEvent("contentSafeAreaChanged", applyInset)
  applyInset()
}

export function haptic() {
  try { tg()?.HapticFeedback?.impactOccurred("light") } catch { /* 桌面端可能没有 */ }
}

function onBack() { backFn?.() }
function onMain() { mainFn?.() }

export function setBack(handler: (() => void) | null) {
  const button = tg()?.BackButton
  backFn = handler
  if (!button) return
  if (!backBound) {
    button.onClick(onBack)
    backBound = true
  }
  if (handler) button.show()
  else button.hide()
}

export function setMain(text: string | null, handler?: () => void, loading = false, danger = false) {
  const button = tg()?.MainButton
  mainFn = text ? handler || null : null
  if (!button) return
  if (!mainBound) {
    button.onClick(onMain)
    mainBound = true
  }
  if (!text) {
    button.hideProgress?.()
    button.hide()
    return
  }
  button.setParams?.({
    text,
    color: danger ? "#D54941" : "#0052D9",
    text_color: "#ffffff",
    is_active: !loading,
    is_visible: true,
  })
  if (loading) button.showProgress?.(false)
  else button.hideProgress?.()
  button.show()
}
