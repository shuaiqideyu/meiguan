import { reactive } from "vue"
import { Toast } from "tdesign-mobile-vue"

export const flow = reactive({
  show: false,
  text: "",
  state: "" as "" | "run" | "ok" | "bad",
})

let pending = 0

function toastLines(text: string) {
  const parts = text.split(/(?<=[。！？])/u).map((item) => item.trim()).filter(Boolean)
  return parts.length ? parts.join("\n") : text
}

export async function runFlow(doing: string, done: string, job: () => Promise<string | void>) {
  pending += 1
  flow.show = true
  flow.state = "run"
  flow.text = doing
  Toast.loading({ message: toastLines(doing), duration: 0, preventScrollThrough: false })
  try {
    const next = await job()
    flow.state = "ok"
    flow.text = next || done
  } catch (err) {
    flow.state = "bad"
    flow.text = err instanceof Error ? err.message : "处理失败"
    throw err
  } finally {
    pending -= 1
    if (pending > 0) return
    const stay = flow.state === "ok" ? (flow.text.length > 8 ? 2200 : 700) : 1400
    if (flow.state === "ok") Toast.success({ message: toastLines(flow.text), duration: stay })
    else Toast.error({ message: toastLines(flow.text), duration: stay })
    window.setTimeout(() => {
      if (pending === 0) flow.show = false
    }, stay)
  }
}
