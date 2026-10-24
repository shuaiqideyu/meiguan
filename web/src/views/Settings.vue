<script setup lang="ts">
import { onMounted, ref, watch } from "vue"
import { useRoute, useRouter } from "vue-router"
import { ChatAddIcon } from "tdesign-icons-vue-next"
import { home, loadHome } from "../state"
import Hot from "./Hot.vue"
import Modules from "./Modules.vue"
import Money from "./Money.vue"
import Windows from "./Windows.vue"

const route = useRoute()
const router = useRouter()
const tabs = ["mode", "shield", "money", "hot"]

function tabOf(value: unknown) {
  const name = String(value || "")
  return tabs.includes(name) ? name : "mode"
}

const tab = ref(tabOf(route.query.tab))
const windows = ref<{ openAdd: () => void } | null>(null)
const hot = ref<{ openAdd: () => void } | null>(null)
const canAdd = () => tab.value === "shield" || tab.value === "hot"

function addWindow() {
  if (tab.value === "shield") windows.value?.openAdd()
  else hot.value?.openAdd()
}

onMounted(() => {
  if (!home.loaded) loadHome()
  window.scrollTo(0, 0)
})

watch(tab, (value) => {
  window.scrollTo(0, 0)
  if (route.path !== "/settings" || tabOf(route.query.tab) === value) return
  router.replace({ path: "/settings", query: { tab: value } })
})

watch(() => route.query.tab, (value) => {
  if (route.path !== "/settings") return
  const next = tabOf(value)
  if (tab.value !== next) tab.value = next
})
</script>

<template>
  <main class="page config dock-page" :class="{ 'has-fab': canAdd() }">
    <div class="config-head">
      <t-navbar :fixed="false" title="红包配置" :safe-area-inset-top="false" />
    </div>
    <p v-if="home.loading && !home.error" class="hint">正在加载</p>
    <div v-else-if="home.error && !home.policy" class="state">
      <t-empty :description="home.error" />
      <t-button theme="primary" @click="loadHome">重试</t-button>
    </div>
    <t-tabs v-else v-model="tab" :swipeable="false">
      <t-tab-panel value="mode" label="红包模块">
        <Modules />
      </t-tab-panel>
      <t-tab-panel value="shield" label="窗口屏蔽">
        <Windows ref="windows" :live="tab === 'shield'" />
      </t-tab-panel>
      <t-tab-panel value="money" label="领取阈值">
        <Money />
      </t-tab-panel>
      <t-tab-panel value="hot" label="自动炒群">
        <Hot ref="hot" embedded />
      </t-tab-panel>
    </t-tabs>
    <div v-if="canAdd()" class="account-fab">
      <div class="account-fab-stack">
        <button type="button" class="account-fab-main" aria-label="添加窗口" @click="addWindow">
          <ChatAddIcon size="26px" />
        </button>
      </div>
    </div>
  </main>
</template>
