import { createApp } from "vue"
import App from "./App.vue"
import { bootChrome } from "./chrome"
import router from "./router"
import "tdesign-mobile-vue/dist/reset.css"
import "./theme.css"

bootChrome()
createApp(App).use(router).mount("#app")
