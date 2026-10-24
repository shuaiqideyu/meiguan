<script setup lang="ts">
import { onMounted, ref } from "vue"
import { loadHome } from "./state"

const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches
const meltMs = reduce ? 0 : 420
const phase = ref<"in" | "on" | "melt" | "done">("in")
const pct = ref(0)

onMounted(() => {
  requestAnimationFrame(() => {
    phase.value = "on"
  })
  loadHome((ratio) => {
    pct.value = Math.round(ratio * 100)
  }).catch(() => {}).then(() => {
    phase.value = "melt"
    window.setTimeout(() => {
      phase.value = "done"
    }, meltMs)
  })
})
</script>

<template>
  <div
    v-if="phase !== 'done'"
    class="gate"
    :class="phase"
    :style="{ '--melt': `${meltMs}ms` }"
    role="status"
    aria-live="polite"
  >
    <div class="shutter"></div>
    <div class="mark">
      <p class="line">没关就是没开</p>
      <p class="by">@yuuu</p>
      <div class="meter" role="progressbar" :aria-valuenow="pct" aria-valuemin="0" aria-valuemax="100" aria-label="正在加载">
        <span class="meter-fill" :style="{ width: `${pct}%` }"></span>
      </div>
      <p class="pct">{{ pct }}%</p>
    </div>
  </div>
</template>

<style scoped>
.gate {
  position: fixed;
  inset: 0;
  z-index: 5600;
  overflow: hidden;
  background: transparent;
  pointer-events: auto;
}

.gate.melt {
  opacity: 0;
  pointer-events: none;
  transition: opacity var(--melt) ease-in-out;
}

.shutter {
  position: absolute;
  inset: 0;
  background: repeating-linear-gradient(
    to bottom,
    #1a1a1a 0 14px,
    #222 14px 16px
  );
  pointer-events: none;
}

.mark {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  box-sizing: border-box;
  padding: var(--safe-top, 0px) 24px var(--safe-bottom, 0px);
  color: #fff;
  text-align: center;
}

.line {
  margin: 0;
  font-size: clamp(18px, 5.6vw, 22px);
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: 0.08em;
  white-space: nowrap;
  opacity: 0;
}

.line::after {
  content: "";
  display: block;
  width: 28px;
  height: 2px;
  margin: 14px auto 0;
  background: #0052d9;
  transform: scaleX(0);
}

.by {
  margin: 16px 0 0;
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 0.12em;
  opacity: 0;
}

.meter {
  width: min(220px, 64vw);
  height: 3px;
  margin: 22px 0 0;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.28);
}

.meter-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: #fff;
  transition: width 0.18s linear;
}

.pct {
  margin: 8px 0 0;
  color: rgba(255, 255, 255, 0.8);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.04em;
  opacity: 0;
}

.gate.on .line,
.gate.on .by,
.gate.on .pct,
.gate.melt .line,
.gate.melt .by,
.gate.melt .pct {
  opacity: 1;
  transition: opacity 0.36s ease-in-out 0.24s;
}

.gate.on .by,
.gate.melt .by {
  transition-delay: 0.4s;
}

.gate.on .line::after,
.gate.melt .line::after {
  transform: scaleX(1);
  transition: transform 0.36s ease-in-out 0.32s;
}

@media (prefers-reduced-motion: reduce) {
  .gate,
  .shutter,
  .mark,
  .line,
  .line::after,
  .by {
    transition: none;
  }

  .gate.melt {
    opacity: 1;
  }

  .gate.melt .mark,
  .gate.on .line,
  .gate.on .by,
  .gate.on .pct,
  .gate.on .line::after {
    opacity: 1;
    transform: none;
  }
}
</style>
