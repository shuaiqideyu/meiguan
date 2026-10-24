import { createRouter, createWebHistory } from "vue-router"
import Accounts from "./views/Accounts.vue"
import Account from "./views/Account.vue"
import Settings from "./views/Settings.vue"
import Claims from "./views/Claims.vue"
import Delay from "./views/Delay.vue"
import Hot from "./views/Hot.vue"
import Floor from "./views/Floor.vue"
import Hours from "./views/Hours.vue"
import Miss from "./Miss.vue"

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: Accounts, meta: { tab: "accounts" } },
    { path: "/account/:node/:id", component: Account, meta: { back: true } },
    { path: "/import", redirect: "/" },
    { path: "/settings", component: Settings, meta: { tab: "settings" } },
    { path: "/modules", redirect: "/settings" },
    { path: "/windows", redirect: "/settings" },
    { path: "/prefs", redirect: "/settings" },
    { path: "/prefs/delay", redirect: "/settings" },
    { path: "/modules/cloudflare", redirect: "/modules/webapp" },
    { path: "/modules/:kind", component: Delay, meta: { back: true } },
    { path: "/hot", component: Hot, meta: { back: true } },
    { path: "/hot/pick", redirect: { path: "/settings", query: { tab: "hot" } } },
    { path: "/claims", component: Claims, meta: { tab: "claims" } },
    { path: "/prefs/money", redirect: "/settings" },
    { path: "/prefs/money/:code", component: Floor, meta: { back: true } },
    { path: "/prefs/hours", component: Hours, meta: { back: true } },
    {
      path: "/:pathMatch(.*)*",
      name: "miss",
      component: Miss,
      meta: { back: true },
      props: {
        code: "404",
        title: "没有这个页面",
        text: "这个地址不存在。",
        action: "回到账号",
      },
    },
  ],
})

export default router
