import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import ProjectsView from '../views/ProjectsView.vue'
import SettingsView from '../views/SettingsView.vue'
import SkillsView from '../views/SkillsView.vue'
import UsageView from '../views/UsageView.vue'
import CommitView from '../views/CommitView.vue'
import { setNavigateHandler, settingsSection, store } from '../store'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/dash',
  },
  {
    path: '/dash',
    name: 'dash',
    component: DashboardView,
    meta: { title: '看板' },
  },
  {
    path: '/projects',
    name: 'projects',
    component: ProjectsView,
    meta: { title: '项目' },
  },
  {
    // 人工把关提交子页（项目页的子路由语义）：左侧改动文件清单 + 右侧 diff 对比
    path: '/commit',
    name: 'commit',
    component: CommitView,
    meta: { title: '人工把关提交' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: SettingsView,
    meta: { title: '设置' },
  },
  {
    path: '/skills',
    name: 'skills',
    component: SkillsView,
    meta: { title: '技能' },
  },
  {
    path: '/usage',
    name: 'usage',
    component: UsageView,
    meta: { title: '用量' },
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/dash',
  },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 路由与 store.view 及设置页分区状态保持双向同步
router.afterEach((to) => {
  if (to.name === 'commit') {
    // 提交页是「项目」页的子页面：保持顶部「项目」Tab 高亮，不引入第六个 Tab
    store.view = 'projects'
  } else if (to.name && typeof to.name === 'string') {
    store.view = to.name as typeof store.view
  }
  if (to.name === 'settings' && to.query.section) {
    settingsSection.value = String(to.query.section)
  }
})

// 监听编程式导航回写（name 可为任意路由名，含非 Tab 子页 commit）
setNavigateHandler((name, query) => {
  const cur = router.currentRoute.value
  const q = query ?? {}
  const sameName = cur.name === name
  const sameQuery = Object.entries(q).every(
    ([k, v]) => String(cur.query[k] ?? '') === String(v ?? ''))
  if (!sameName || !sameQuery) void router.push({ name, query })
})
