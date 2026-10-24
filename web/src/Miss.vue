<script setup lang="ts">
import { onMounted, watch } from "vue"
import { useRouter } from "vue-router"
import { haptic } from "./chrome"

const props = defineProps<{
  code?: string
  title: string
  text: string
  action: string
  href?: string
}>()

const router = useRouter()

function applyTitle() {
  document.title = props.title
}

onMounted(applyTitle)
watch(() => props.title, applyTitle)

function go() {
  haptic()
  router.replace("/")
}
</script>

<template>
  <main class="miss">
    <p v-if="code" class="code">{{ code }}</p>
    <h1>{{ title }}</h1>
    <span class="rule" aria-hidden="true"></span>
    <p class="text">{{ text }}</p>
    <a v-if="href" class="go" :href="href">{{ action }}</a>
    <button v-else class="go" type="button" @click="go">{{ action }}</button>
  </main>
</template>

<style scoped>
.miss {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  min-height: var(--app-height, 100dvh);
  padding:
    calc(32px + max(var(--safe-top, 0px), env(safe-area-inset-top, 0px)))
    24px
    calc(32px + max(var(--safe-bottom, 0px), env(safe-area-inset-bottom, 0px)));
  background: #f3f3f3;
  text-align: center;
}

.code {
  margin: 0 0 8px;
  color: rgba(0, 0, 0, 0.55);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.16em;
}

h1 {
  margin: 0;
  color: rgba(0, 0, 0, 0.9);
  font-size: 20px;
  font-weight: 600;
  line-height: 1.35;
  letter-spacing: 0.02em;
}

.rule {
  display: block;
  width: 28px;
  height: 2px;
  margin: 14px 0 0;
  background: #0052d9;
}

.text {
  max-width: 16em;
  margin: 12px 0 0;
  color: rgba(0, 0, 0, 0.6);
  font-size: 14px;
  line-height: 1.5;
  white-space: pre-line;
}

.go {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  min-width: 148px;
  min-height: 44px;
  margin-top: 28px;
  padding: 0 22px;
  border: 0;
  border-radius: 6px;
  background: #0052d9;
  color: #fff;
  font: inherit;
  font-size: 16px;
  font-weight: 600;
  line-height: 1;
  text-decoration: none;
  cursor: pointer;
}

.go:hover {
  background: #366ef4;
}

.go:active {
  background: #003cab;
}

.go:focus-visible {
  outline: 2px solid #0052d9;
  outline-offset: 3px;
}
</style>
