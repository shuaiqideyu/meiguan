import { reactive } from "vue"

export const liquid = reactive({ show: false })

let depth = 0
let arm = 0
let fade = 0

export function holdLiquid() {
  depth += 1
  window.clearTimeout(fade)
  fade = 0
  if (!liquid.show && !arm) {
    arm = window.setTimeout(() => {
      arm = 0
      if (depth > 0) liquid.show = true
    }, 180)
  }
  let released = false
  return () => {
    if (released) return
    released = true
    depth = Math.max(0, depth - 1)
    if (depth > 0) return
    window.clearTimeout(arm)
    arm = 0
    if (!liquid.show) return
    fade = window.setTimeout(() => {
      fade = 0
      if (depth === 0) liquid.show = false
    }, 320)
  }
}
