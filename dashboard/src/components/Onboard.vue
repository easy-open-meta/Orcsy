<script setup lang="ts">
import { ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { tNow as it } from '../lang'
import { ui } from '../store'

// 首次使用引导动画：分步卡片 + 渐变光球 + 浮动图标 + 滑入切换。
// 首启判定：localStorage 无 ta-onboarded 标记（App.vue 登录态就绪后自动打开）；
// 完成/跳过写标记，之后不再自动弹出；顶栏「?」与移动抽屉「使用引导」可随时重看。
const STORAGE_KEY = 'ta-onboarded'

const STEPS: { icon: string; title: string; desc: string }[] = [
  {
    icon: 'waving_hand',
    title: '欢迎使用 Orcsy',
    desc: '把开发与修复任务交给 Agent：按优先级自动领取、编码、测试、独立审查，全程留痕可回溯。',
  },
  {
    icon: 'smart_toy',
    title: '接入大模型与小组',
    desc: '「设置」页先接入大模型（协议 / 模型 / API Key）——这是一切功能的基础和底座；再按需启用「多模型项目小组」，为 PM、后端、前端、QA 指定专属模型与能力边界。',
  },
  {
    icon: 'folder',
    title: '创建项目',
    desc: '「项目」页新建项目，填入代码仓库路径；自动生成 AGENT.md 指引与任务目录，Agent 即可开始工作。',
  },
  {
    icon: 'playlist_add',
    title: '添加任务',
    desc: '项目卡上输入标题回车快速添加；拖拽任务还能调整执行顺序（同一优先级内生效）。',
  },
  {
    icon: 'play_arrow',
    title: '一键扫描',
    desc: '点顶部「立即扫描」启动一轮执行：领任务 → 改代码 → 跑测试 → 独立审查 → 合入主工作区。',
  },
  {
    icon: 'rule',
    title: '人工把关',
    desc: '成果不会自动提交：到「人工把关提交」页确认 diff、写提交说明（可 AI 生成），由你完成最终提交。',
  },
  {
    icon: 'smartphone',
    title: '手机远程管理',
    desc: '手机与电脑连同一 Wi-Fi 扫码直达看板，随时查看进度、处理审批、新建任务。',
  },
]

const step = ref(0)

watch(() => ui.onboard, (open) => {
  if (open) step.value = 0
})

function next(): void {
  if (step.value >= STEPS.length - 1) {
    finish()
    return
  }
  step.value++
}

function prev(): void {
  if (step.value > 0) step.value--
}

function finish(): void {
  try {
    localStorage.setItem(STORAGE_KEY, '1')
  } catch { /* 隐私模式禁用存储：仅本次会话内不再自动弹 */ }
  ui.onboard = false
}
</script>

<template>
  <Transition name="ob-fade">
    <div v-if="ui.onboard" class="ob-mask" role="dialog" :aria-label="it('使用引导')">
      <!-- 背景渐变光球：慢速漂移动画 -->
      <i class="ob-orb o1" /><i class="ob-orb o2" /><i class="ob-orb o3" />
      <div class="ob-card">
        <div class="ob-head">
          <div class="ob-icon"><i class="ms">{{ STEPS[step].icon }}</i></div>
          <div class="ob-dots">
            <i v-for="(st, i) in STEPS" :key="st.icon" class="ob-dot"
               :class="{ on: i === step, done: i < step }" />
          </div>
        </div>

        <Transition name="ob-step" mode="out-in">
          <div :key="step" class="ob-step">
            <h2>{{ it(STEPS[step].title) }}</h2>
            <p>{{ it(STEPS[step].desc) }}</p>
          </div>
        </Transition>

        <div class="ob-actions">
          <NButton quaternary size="small" @click="finish">{{ it('跳过引导') }}</NButton>
          <span class="ob-spacer" />
          <NButton v-if="step > 0" size="small" @click="prev">{{ it('上一步') }}</NButton>
          <NButton type="primary" size="small" @click="next">
            {{ step === STEPS.length - 1 ? it('开始使用') : it('下一步') }}
          </NButton>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.ob-mask {
  position: fixed; inset: 0; z-index: 2500;
  display: flex; align-items: center; justify-content: center;
  background: rgba(8, 12, 22, 0.72); backdrop-filter: blur(6px);
}
/* 渐变光球：模糊大圆慢速漂移，营造引导动画氛围 */
.ob-orb {
  position: absolute; border-radius: 50%; filter: blur(60px); opacity: .35;
  pointer-events: none; animation: ob-float 9s ease-in-out infinite;
}
.ob-orb.o1 { width: 320px; height: 320px; left: 8%; top: 12%;
  background: radial-gradient(circle, #5b8cff, transparent 70%); }
.ob-orb.o2 { width: 260px; height: 260px; right: 6%; top: 20%;
  background: radial-gradient(circle, #9a6bff, transparent 70%); animation-delay: -3s; }
.ob-orb.o3 { width: 300px; height: 300px; left: 30%; bottom: 4%;
  background: radial-gradient(circle, #3ec7c2, transparent 70%); animation-delay: -6s; }
@keyframes ob-float {
  0%, 100% { transform: translate(0, 0) scale(1); }
  50% { transform: translate(18px, -22px) scale(1.06); }
}

.ob-card {
  position: relative; width: min(520px, 92vw);
  background: var(--card, #1a2235); border: 1px solid var(--line, #253048);
  border-radius: 16px; padding: 26px 28px 20px;
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.4);
}
.ob-head { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
.ob-icon {
  width: 58px; height: 58px; border-radius: 16px; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #5b8cff, #9a6bff);
}
.ob-icon .ms {
  font-size: 30px; color: #fff; animation: ob-pop .55s cubic-bezier(.2, .9, .3, 1.3);
}
@keyframes ob-pop {
  from { transform: scale(.55) rotate(-12deg); opacity: 0; }
  to { transform: scale(1) rotate(0); opacity: 1; }
}
.ob-dots { display: flex; gap: 7px; flex: 1; }
.ob-dot {
  width: 8px; height: 8px; border-radius: 99px; background: var(--line, #3a4664);
  transition: width .35s ease, background .35s ease;
}
.ob-dot.done { background: #5b8cff88; }
.ob-dot.on { width: 26px; background: linear-gradient(90deg, #5b8cff, #9a6bff); }

.ob-step h2 { margin: 0 0 8px; font-size: 19px; color: var(--text); }
.ob-step p {
  margin: 0; min-height: 66px; font-size: 13.5px; line-height: 1.8;
  color: var(--dim, #94a2be);
}

/* 步骤切换：上滑淡出/进入 */
.ob-step-enter-active { transition: all .32s ease; }
.ob-step-leave-active { transition: all .2s ease; }
.ob-step-enter-from { opacity: 0; transform: translateY(16px); }
.ob-step-leave-to { opacity: 0; transform: translateY(-10px); }

.ob-actions { display: flex; align-items: center; gap: 8px; margin-top: 16px; }
.ob-spacer { flex: 1; }

.ob-fade-enter-active { transition: opacity .3s ease; }
.ob-fade-leave-active { transition: opacity .22s ease; }
.ob-fade-enter-from, .ob-fade-leave-to { opacity: 0; }

@media (max-width: 768px) {
  .ob-card { padding: 22px 18px 16px; }
  .ob-step p { min-height: 88px; }
}
</style>
