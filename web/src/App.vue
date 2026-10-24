<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from "vue"
import { useRoute, useRouter } from "vue-router"
import { TimeIcon, UserIcon, ViewModuleIcon } from "tdesign-icons-vue-next"
import { haptic, openedInTelegram, setBack, setMain } from "./chrome"
import Gate from "./Gate.vue"
import Liquid from "./Liquid.vue"
import Miss from "./Miss.vue"

const outside = !openedInTelegram()
const doorText = "私聊机器人，点「打开」。\n左下角「管理」也可以。"

const route = useRoute()
const router = useRouter()

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push("/")
}

const dock = computed(() => {
  const tab = String(route.meta.tab || "")
  if (tab === "settings") return 1
  if (tab === "claims") return 2
  return 0
})

function onTab(value: string | number) {
  const path = value === "settings" ? "/settings" : value === "claims" ? "/claims" : "/"
  if (route.path === path) return
  haptic()
  router.replace(path)
}

watch(
  () => route.fullPath,
  () => {
    if (!outside && route.name !== "miss") document.title = "管理"
    setMain(null)
    setBack(!outside && route.meta.back ? goBack : null)
  },
  { immediate: true },
)

onMounted(() => {
  if (!outside) setBack(route.meta.back ? goBack : null)
})
onUnmounted(() => {
  setBack(null)
  setMain(null)
})
</script>

<template>
  <Miss
    v-if="outside"
    title="请从 Telegram 打开"
    :text="doorText"
    action="打开机器人"
    href="https://t.me/meikaiguabot"
  />
  <template v-else>
    <Gate v-if="route.name !== 'miss'" />
    <Liquid />
    <router-view />
    <div v-if="route.meta.tab" class="dock" :style="{ '--dock': dock }">
      <t-tab-bar
        :value="String(route.meta.tab)"
        :placeholder="true"
        :split="false"
        :bordered="false"
        shape="round"
        :z-index="30"
        @change="onTab"
      >
        <t-tab-bar-item value="accounts">
          <template #icon><UserIcon /></template>
          账号列表
        </t-tab-bar-item>
        <t-tab-bar-item value="settings">
          <template #icon><ViewModuleIcon /></template>
          红包配置
        </t-tab-bar-item>
        <t-tab-bar-item value="claims">
          <template #icon><TimeIcon /></template>
          领取记录
        </t-tab-bar-item>
      </t-tab-bar>
    </div>
  </template>
</template>
