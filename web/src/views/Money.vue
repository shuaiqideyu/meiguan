<script setup lang="ts">
import { useRouter } from "vue-router"
import { ref } from "vue"
import { COINS, coinLabel } from "../api"
import { haptic } from "../chrome"
import { home, loadHome, patch } from "../state"

const router = useRouter()
const error = ref("")
if (!home.loaded) loadHome()

function shown(code: string) {
  const count = home.policy?.min_counts?.[code] || ""
  const total = home.policy?.min_totals?.[code] || ""
  const each = home.policy?.min_eaches?.[code] || ""
  const parts: string[] = []
  if (count && count !== "不限制") parts.push(`个数 ${count}`)
  if (total && total !== "不限制") parts.push(`金额 ${total}`)
  if (each && each !== "不限制") parts.push(`人均 ${each}`)
  return parts.join(" · ") || "不限制"
}

async function coin(code: string) {
  if (!home.policy) return
  haptic()
  const next = home.policy.coins[code] === false
  home.policy.coins[code] = next
  error.value = ""
  try {
    await patch({ coins: { ...home.policy.coins } })
  } catch (err) {
    home.policy.coins[code] = !next
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

async function over() {
  if (!home.policy) return
  haptic()
  const next = home.policy.count_over === false
  home.policy.count_over = next
  error.value = ""
  try {
    await patch({ count_over: next })
  } catch (err) {
    home.policy.count_over = !next
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}

async function unknown() {
  if (!home.policy) return
  haptic()
  const next = !home.policy.unknown_coin
  home.policy.unknown_coin = next
  error.value = ""
  try {
    await patch({ unknown_coin: next })
  } catch (err) {
    home.policy.unknown_coin = !next
    error.value = err instanceof Error ? err.message : "处理失败"
  }
}
</script>

<template>
  <section>
    <t-notice-bar v-if="error" theme="error" :content="error" />
    <t-cell-group v-if="home.policy">
      <t-cell title="个数大于金额不抢" description="按金额的整数比；1.23 发100个不抢。">
        <t-switch :model-value="home.policy.count_over !== false" @change="over" />
      </t-cell>
      <t-cell v-for="code in COINS" :key="code" :title="coinLabel(code)" :description="shown(code)" arrow hover @click="router.push(`/prefs/money/${code}`)">
        <div @click.stop>
          <t-switch :model-value="home.policy.coins[code] !== false" @change="coin(code)" />
        </div>
      </t-cell>
      <t-cell title="未知币种">
        <t-switch :model-value="home.policy.unknown_coin" @change="unknown" />
      </t-cell>
    </t-cell-group>
  </section>
</template>
