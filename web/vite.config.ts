import vue from "@vitejs/plugin-vue"
import { TDesignResolver } from "@tdesign-vue-next/auto-import-resolver"
import AutoImport from "unplugin-auto-import/vite"
import Components from "unplugin-vue-components/vite"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [TDesignResolver({ library: "mobile-vue" })],
      dts: false,
    }),
    Components({
      resolvers: [TDesignResolver({ library: "mobile-vue" })],
      dts: false,
    }),
  ],
  build: { outDir: "dist", emptyOutDir: true },
})
