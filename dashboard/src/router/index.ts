import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import ProjectsView from '../views/ProjectsView.vue'
import SettingsView from '../views/SettingsView.vue'
import SkillsView from '../views/SkillsView.vue'
import UsageView from '../views/UsageView.vue'
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
  if (to.name && typeof to.name === 'string') {
    store.view = to.name as typeof store.view
  }
  if (to.name === 'settings' && to.query.section) {
    settingsSection.value = String(to.query.section)
  }
})

// 监听编程式导航回写
setNavigateHandler((name, query) => {
  if (router.currentRoute.value.name !== name || (query && query.section !== router.currentRoute.value.query.section)) {
    void router.push({ name, query })
  }
})
