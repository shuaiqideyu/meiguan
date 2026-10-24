<script setup lang="ts">
import { liquid } from "./liquid"
</script>

<template>
  <div class="liquid" :class="{ on: liquid.show }" :aria-hidden="liquid.show ? 'false' : 'true'" role="status" aria-live="polite" aria-label="正在加载">
    <span class="track"></span>
    <span class="body"></span>
    <span class="head"></span>
  </div>
</template>

<style scoped>
.liquid {
  position: fixed;
  top: 0;
  left: 0;
  z-index: 5500;
  width: 100%;
  height: 4px;
  overflow: hidden;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s linear;
}

.liquid.on {
  opacity: 1;
}

.track,
.body,
.head {
  position: absolute;
  top: 0;
  left: 0;
  height: 4px;
}

.track {
  width: 100%;
  background: rgba(0, 82, 217, 0.16);
}

.body,
.head {
  border-radius: 999px;
  transform-origin: center;
  animation: tide 1.35s cubic-bezier(0.37, 0, 0.23, 1) infinite;
}

.body {
  width: 46vw;
  background: linear-gradient(
    90deg,
    rgba(0, 82, 217, 0) 0%,
    rgba(0, 82, 217, 0.45) 28%,
    #4d8dff 68%,
    rgba(0, 82, 217, 0) 100%
  );
}

.head {
  width: 18vw;
  background: linear-gradient(
    90deg,
    rgba(0, 82, 217, 0) 0%,
    #0052d9 16%,
    #6aa3ff 40%,
    #e7f0ff 50%,
    #6aa3ff 60%,
    #0052d9 84%,
    rgba(0, 82, 217, 0) 100%
  );
  animation-delay: -0.16s;
}

.liquid:not(.on) .body,
.liquid:not(.on) .head {
  animation-play-state: paused;
}

@keyframes tide {
  0% { transform: translate3d(-55vw, 0, 0) scaleX(0.72); }
  46% { transform: translate3d(38vw, 0, 0) scaleX(1.22); }
  100% { transform: translate3d(108vw, 0, 0) scaleX(0.64); }
}

@media (prefers-reduced-motion: reduce) {
  .body,
  .head {
    animation: none;
  }

  .body { display: none; }

  .liquid.on .head {
    width: 100%;
    transform: none;
    opacity: 0.9;
  }
}
</style>
