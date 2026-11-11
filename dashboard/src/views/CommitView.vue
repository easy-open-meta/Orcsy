// 人工把关提交页：左侧改动文件清单，右侧代码 diff 对比（独立标签页，非弹窗）。
// 入口：看板/项目卡片「待人工提交」按钮 → openCommitDlg(project) → /#/commit?project=xxx
<script setup lang="ts">
import { NButton, NInput, NSpin, NSwitch, NTag } from 'naive-ui'
import { computed, onActivated, reactive, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { refresh, store } from '../store'

const route = useRoute()
const router = useRouter()

const projectName = computed(() => String(route.query.project || ''))
const project = computed(() =>
  store.state?.projects.find(p => p.name === projectName.value) ?? null)
const files = computed<string[]>(() => project.value?.pending_git_changes?.files ?? [])

// 当前选中文件与逐文件 diff 缓存（切回不重复请求；重新进入页面时清空重拉）
const selected = shallowRef('')
const diffCache = reactive<Record<string, string>>({})
const diffMeta = reactive<Record<string, { truncated: boolean; totalChars: number; lines: number }>>({})
const loading = shallowRef(false)
const loadErr = shallowRef('')
// 自动换行：默认开启，长行折行显示，避免需要横向拖动才能看全
const wrapLines = shallowRef(true)

// 待排除文件集合：勾选后不纳入本次提交（保留在工作区，可随时恢复）
const excluded = reactive<Record<string, boolean>>({})
const excludedList = computed(() => files.value.filter(f => excluded[f]))
const includedList = computed(() => files.value.filter(f => !excluded[f]))

const form = reactive({ message: '', push: false, submitting: false, err: '' })

// ---- 临时/测试产物识别：一键排除，避免把测试跑出来的临时文件带进主分支 ----
// 保守白名单式匹配（只匹配明确属于构建/测试缓存与临时输出的产物），
// 绝不匹配 *_test.go / *.spec.ts 这类真实的测试源码（那些应当提交）。
const TEMP_PATTERNS: RegExp[] = [
  /(^|\/)__pycache__(\/|$)/i,
  /(^|\/)\.pytest_cache(\/|$)/i,
  /(^|\/)\.mypy_cache(\/|$)/i,
  /(^|\/)\.ruff_cache(\/|$)/i,
  /(^|\/)node_modules(\/|$)/i,
  /(^|\/)\.coverage(\/|$)/i,
  /(^|\/)htmlcov(\/|$)/i,
  /(^|\/)\.nyc_output(\/|$)/i,
  /(^|\/)coverage(\/|$)/i,
  /(^|\/)tmp(\/|$)/i,
  /(^|\/)\.tmp(\/|$)/i,
  /\.(pyc|pyo|tmp|temp|log|orig|rej|bak|swp|swo)$/i,
  /(^|\/)(\.DS_Store|Thumbs\.db)$/i,
]

function looksTemporary(file: string): boolean {
  return TEMP_PATTERNS.some(re => re.test(file))
}

// 建议排除的临时产物：只列出「尚未排除」的，全部排除后按钮自动消失
const suggestedTemp = computed(() => files.value.filter(f => looksTemporary(f) && !excluded[f]))

function toggleExclude(file: string): void {
  if (excluded[file]) delete excluded[file]
  else excluded[file] = true
}

function excludeSuggestedTemp(): void {
  for (const f of suggestedTemp.value) excluded[f] = true
}

function includeAll(): void {
  for (const k of Object.keys(excluded)) delete excluded[k]
}

// ---- 丢弃改动：未跟踪文件物理删除；已跟踪文件还原到 HEAD（弹窗确认，防误删）----
const discarding = shallowRef<string[]>([])

function forgetDiff(files: string[]): void {
  for (const f of files) {
    delete diffCache[f]
    delete diffMeta[f]
    delete excluded[f]
  }
  if (selected.value && files.includes(selected.value)) selected.value = ''
}

async function discardFiles(targets: string[]): Promise<void> {
  if (!targets.length) return
  discarding.value = targets
  try {
    const res = await api.discardProjectFiles({ name: projectName.value, files: targets })
    window.$message?.(res.message || '已丢弃所选改动', 'success')
    forgetDiff(targets)
    await refresh(true)
  } catch (e: any) {
    window.$message?.(e.message, 'error')
  } finally {
    discarding.value = []
  }
}

function confirmDiscard(targets: string[], batch = false): void {
  if (!targets.length) return
  const preview = targets.slice(0, 8).map(f => `  · ${f}`).join('\n')
  const more = targets.length > 8 ? `\n  … 等共 ${targets.length} 项` : ''
  window.$dialog?.warning({
    title: batch ? `删除 ${targets.length} 个临时/测试产物？` : `丢弃该文件的改动？`,
    content: '未跟踪文件将被物理删除（不可恢复）；已跟踪文件将还原到 HEAD（撤销本次改动，文件本身保留）。'
      + `\n\n${preview}${more}`,
    positiveText: '确认丢弃',
    negativeText: '取消',
    async onPositiveClick() {
      await discardFiles(targets)
    },
  })
}

function backToProjects(): void {
  void router.push({ name: 'projects' })
}

async function loadDiff(file: string): Promise<void> {
  if (!file) return
  selected.value = file
  loadErr.value = ''
  if (diffCache[file] !== undefined) return  // 已缓存直接展示
  loading.value = true
  try {
    const r = await api.projectFileDiff({ name: projectName.value, file })
    diffCache[file] = r.diff || '（无改动内容）'
    diffMeta[file] = {
      truncated: !!r.truncated,
      totalChars: Number(r.total_chars ?? 0),
      lines: Number(r.lines ?? 0),
    }
  } catch (e: any) {
    loadErr.value = `加载 diff 失败：${e.message}`
  } finally {
    loading.value = false
  }
}

const curMeta = computed(() => (selected.value ? diffMeta[selected.value] : undefined))

// 下载当前文件完整补丁（.patch）：内容超出预览上限时可借此查看/归档全量 diff
function downloadPatch(): void {
  const f = selected.value
  if (!f || diffCache[f] === undefined) return
  const safeName = f.replace(/[\\/:*?"<>|]/g, '_')
  const blob = new Blob([diffCache[f]], { type: 'text/plain;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${safeName}.patch`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(a.href), 3000)
}

function resetPage(): void {
  for (const k of Object.keys(diffCache)) delete diffCache[k]
  for (const k of Object.keys(diffMeta)) delete diffMeta[k]
  for (const k of Object.keys(excluded)) delete excluded[k]
  selected.value = ''
  loading.value = false
  loadErr.value = ''
  form.message = ''
  form.push = false
  form.submitting = false
  form.err = ''
  const first = files.value.find(f => !excluded[f])
  if (first) void loadDiff(first)
}

// KeepAlive 页面：每次进入（含首次挂载）都按当前路由 project 重置并拉最新状态
onActivated(() => {
  resetPage()
  void refresh(true)
})

// 待提交文件列表变化（如轮询刷新、提交后清零）：自动校正选中项
watch(files, (list) => {
  if (!list.length) { selected.value = ''; return }
  // 已排除的文件从列表中消失时，同步清掉它的排除标记
  for (const k of Object.keys(excluded)) {
    if (!list.includes(k)) delete excluded[k]
  }
  const target = list.find(f => !excluded[f])
  if (!target) { selected.value = ''; return }
  if (!selected.value || !list.includes(selected.value) || excluded[selected.value]) {
    void loadDiff(target)
  }
})

async function submit(): Promise<void> {
  if (!project.value) return
  if (!form.message.trim()) { form.err = '请填写 Commit Message 提交说明'; return }
  if (!includedList.value.length) {
    form.err = '所有文件都被排除，至少保留一个文件才能提交'
    return
  }
  form.submitting = true
  form.err = ''
  try {
    const res = await api.commitProject({
      name: projectName.value,
      message: form.message.trim(),
      push: form.push,
      exclude: excludedList.value,
    })
    window.$message?.(res.message || '已成功提交并把关入库！', 'success')
    await refresh(true)
    // 全部提交完毕（没有剩余排除项）才回到项目页；还有排除文件时留在本页，
    // 让用户明确看到哪些文件未提交（列表会自动刷新为剩余文件）
    if (!(res.skipped && res.skipped.length)) backToProjects()
  } catch (e: any) {
    form.err = e.message
  } finally {
    form.submitting = false
  }
}
</script>

<template>
  <div class="commitpage">
    <div class="chead">
      <div class="cleft">
        <NButton size="small" quaternary @click="backToProjects">← 返回项目</NButton>
        <span class="ctitle">人工把关提交 · {{ projectName || '（未知项目）' }}</span>
        <NTag v-if="files.length" size="small" type="info" :bordered="false">
          {{ files.length }} 个改动文件
        </NTag>
      </div>
      <span v-if="project" class="hint">确认无误后以规范 commit 提交至当前主分支（可选推送）</span>
    </div>

    <div v-if="!project" class="cempty">
      <div>项目不存在或已被移除。</div>
      <NButton size="small" @click="backToProjects">返回项目页</NButton>
    </div>
    <div v-else-if="!files.length" class="cempty">
      <div>当前没有待提交的改动（可能已完成人工提交，或 Agent 尚未落地代码）。</div>
      <NButton size="small" @click="backToProjects">返回项目页</NButton>
    </div>
    <template v-else>
      <div class="cbody">
        <aside class="cfiles">
          <div class="cfiles-head">
            <span>改动文件（{{ files.length }}）</span>
            <span class="cfile-actions">
              <NButton v-if="suggestedTemp.length" size="tiny" quaternary type="warning"
                       :title="`识别到 ${suggestedTemp.length} 个临时/测试产物，一键排除不提交`"
                       @click="excludeSuggestedTemp">排除临时产物 {{ suggestedTemp.length }}</NButton>
              <NButton v-if="suggestedTemp.length" size="tiny" quaternary type="error"
                       :loading="discarding.length > 0"
                       :title="`删除 ${suggestedTemp.length} 个临时/测试产物（未跟踪物理删除，已跟踪还原）`"
                       @click="confirmDiscard(suggestedTemp, true)">删除临时产物 {{ suggestedTemp.length }}</NButton>
              <NButton v-if="excludedList.length" size="tiny" quaternary
                       @click="includeAll">全部包含</NButton>
            </span>
          </div>
          <div v-for="f in files" :key="f" class="cfile"
               :class="{ active: selected === f, excluded: excluded[f] }"
               :title="f" @click="loadDiff(f)">
            <span class="cfile-name">{{ f }}</span>
            <NButton size="tiny" quaternary class="ex-btn"
                     :type="excluded[f] ? 'primary' : 'warning'"
                     :title="excluded[f] ? '恢复：将纳入本次提交' : '排除：不纳入本次提交（保留在工作区）'"
                     @click.stop="toggleExclude(f)">
              {{ excluded[f] ? '恢复' : '排除' }}
            </NButton>
            <NButton size="tiny" quaternary type="error" class="ex-btn"
                     :loading="discarding.includes(f)"
                     title="删除该文件改动（未跟踪物理删除；已跟踪还原到 HEAD）"
                     @click.stop="confirmDiscard([f])">删除</NButton>
          </div>
        </aside>
        <main class="cdiff">
          <div class="cdiff-head">
            <span class="cdiff-name">{{ selected || '选择左侧文件查看 diff' }}</span>
            <NTag v-if="selected && excluded[selected]" size="small" type="warning" :bordered="false">
              已排除（不提交）
            </NTag>
            <NSpin v-if="loading" size="small" />
            <template v-if="selected && diffCache[selected] !== undefined">
              <span class="cdiff-stat" v-if="curMeta">
                {{ curMeta.lines.toLocaleString() }} 行 · {{ (curMeta.totalChars / 1024).toFixed(1) }} KB
              </span>
              <NButton size="tiny" quaternary :type="wrapLines ? 'primary' : 'default'"
                       :title="wrapLines ? '当前：长行自动折行（点击切换为横向滚动）' : '当前：不折行（点击切换为自动折行）'"
                       @click="wrapLines = !wrapLines">自动换行</NButton>
              <NButton size="tiny" quaternary title="下载当前文件的完整补丁（.patch）"
                       @click="downloadPatch">下载补丁</NButton>
            </template>
          </div>
          <div v-if="curMeta?.truncated" class="cdiff-trunc">
            ⚠ 该文件改动较大（共 {{ curMeta.totalChars.toLocaleString() }} 字符），预览已截断；
            可点右上「下载补丁」查看完整 diff。
          </div>
          <div class="cdiff-body">
            <div v-if="loadErr" class="err">{{ loadErr }}</div>
            <pre v-else-if="selected && diffCache[selected] !== undefined"
                 class="cdiff-pre" :class="{ wrap: wrapLines }">{{ diffCache[selected] }}</pre>
            <div v-else class="cdiff-hint">{{ loading ? '正在加载 diff…' : '选择左侧文件查看该文件的未提交改动 diff' }}</div>
          </div>
        </main>
      </div>

      <div class="cfoot">
        <div v-if="excludedList.length" class="exclude-note">
          ⚠ 已排除 {{ excludedList.length }} 个文件不纳入本次提交（保留在工作区）：
          <span class="exclude-names">{{ excludedList.join('、') }}</span>
        </div>
        <div class="cfoot-row">
          <label class="clabel">提交说明 Commit Message</label>
          <NInput v-model:value="form.message" placeholder="如 feat: 完成用户登录与前后端联调测试" />
        </div>
        <div class="cfoot-row cf-actions">
          <label class="checkline">
            <NSwitch size="small" v-model:value="form.push" />
            <span>同时推送到远程仓库 (git push origin)</span>
          </label>
          <div class="spacer" />
          <span class="commit-count">将提交 {{ includedList.length }} 个文件</span>
          <NButton @click="backToProjects">取消</NButton>
          <NButton type="primary" :loading="form.submitting"
                   :disabled="!includedList.length" @click="submit">确认提交主分支</NButton>
        </div>
        <div v-if="form.err" class="err">{{ form.err }}</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.commitpage {
  padding: 0 20px 16px;
  display: flex;
  flex-direction: column;
  height: calc(100vh - 92px);
  min-height: 420px;
}
.chead {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; padding: 2px 0 10px; flex-wrap: wrap;
}
.cleft { display: flex; align-items: center; gap: 10px; min-width: 0; }
.ctitle { font-size: 14px; font-weight: 600; color: var(--text); }
.hint { font-size: 12px; color: var(--faint); }

.cbody { display: flex; gap: 12px; flex: 1; min-height: 0; }
.cfiles {
  width: 300px; flex: none; overflow-y: auto;
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
}
.cfiles-head {
  position: sticky; top: 0; z-index: 1;
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  padding: 6px 8px 6px 10px; font-size: 12px; font-weight: 600; color: var(--dim);
  background: var(--panel); border-bottom: 1px solid var(--line);
}
.cfile-actions { display: flex; gap: 4px; flex-wrap: wrap; }
.cfile {
  display: flex; align-items: center; gap: 6px;
  font-family: Consolas, monospace; font-size: 11.5px;
  padding: 5px 8px 5px 10px; cursor: pointer; word-break: break-all;
  color: var(--dim); border-bottom: 1px solid var(--line);
}
.cfile:hover { background: rgba(91, 140, 255, 0.10); color: var(--text); }
.cfile.active {
  background: rgba(91, 140, 255, 0.16); color: var(--text);
  border-left: 3px solid #5b8cff;
}
.cfile.excluded .cfile-name { text-decoration: line-through; opacity: 0.55; }
.cfile-name { flex: 1; min-width: 0; }
.ex-btn { flex: none; font-size: 11px; padding: 0 4px; }

.cdiff {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  border: 1px solid var(--line); border-radius: 8px;
  background: var(--panel); overflow: hidden;
}
.cdiff-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--line);
  font-family: Consolas, monospace; font-size: 12px; color: var(--dim);
}
.cdiff-name { flex: 1; min-width: 0; word-break: break-all; }
.cdiff-stat { flex: none; font-size: 11.5px; color: var(--faint); white-space: nowrap; }
.cdiff-trunc {
  padding: 6px 12px; font-size: 12px; line-height: 1.6; color: #d9a441;
  background: rgba(217, 164, 65, 0.10); border-bottom: 1px solid rgba(217, 164, 65, 0.32);
}
.cdiff-body { flex: 1; overflow: auto; min-height: 0; }
.cdiff-pre {
  margin: 0; padding: 10px 12px;
  font: 11.5px/1.6 Consolas, "Cascadia Mono", monospace;
  white-space: pre; color: var(--text); tab-size: 4;
}
.cdiff-pre.wrap {
  white-space: pre-wrap; word-break: break-all; overflow-wrap: anywhere;
}
.cdiff-hint { padding: 28px 16px; text-align: center; color: var(--faint); font-size: 13px; }

.cfoot { margin-top: 12px; flex: none; }
.exclude-note {
  font-size: 12px; line-height: 1.6; color: #d9a441;
  background: rgba(217, 164, 65, 0.10); border: 1px solid rgba(217, 164, 65, 0.35);
  border-radius: 6px; padding: 6px 10px; margin-bottom: 8px; word-break: break-all;
}
.exclude-names { font-family: Consolas, monospace; }
.commit-count { font-size: 12px; color: var(--faint); margin-right: 2px; }
.cfoot-row { margin-bottom: 8px; }
.clabel { display: block; font-size: 12px; font-weight: 600; margin-bottom: 4px; color: var(--text); }
.cf-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.checkline { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--dim); }
.spacer { flex: 1; }
.cempty {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 12px;
  color: var(--faint); font-size: 13px;
}
.err { color: #e88080; font-size: 12.5px; margin-top: 4px; }

@media (max-width: 820px) {
  .cbody { flex-direction: column; }
  .cfiles { width: auto; max-height: 180px; }
}
</style>
