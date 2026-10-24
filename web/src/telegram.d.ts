export {}

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp
    }
  }
}

interface SafeInset {
  top?: number
  bottom?: number
  left?: number
  right?: number
}

interface TelegramButton {
  show: () => void
  hide: () => void
  onClick: (fn: () => void) => void
  offClick: (fn: () => void) => void
  setParams?: (params: Record<string, unknown>) => void
  showProgress?: (leave?: boolean) => void
  hideProgress?: () => void
  isVisible?: boolean
}

interface TelegramWebApp {
  ready: () => void
  expand: () => void
  disableVerticalSwipes?: () => void
  initData: string
  platform?: string
  viewportStableHeight?: number
  safeAreaInset?: SafeInset
  contentSafeAreaInset?: SafeInset
  BackButton?: TelegramButton
  MainButton?: TelegramButton
  HapticFeedback?: { impactOccurred: (style: string) => void }
  openTelegramLink?: (url: string) => void
  openLink?: (url: string) => void
  setHeaderColor: (color: string) => void
  setBackgroundColor: (color: string) => void
  setBottomBarColor?: (color: string) => void
  onEvent: (name: string, fn: () => void) => void
  offEvent: (name: string, fn: () => void) => void
}
