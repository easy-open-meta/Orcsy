<script setup lang="ts">
// 审批 Provider 操作差异提示：四家厂商的「批准/拒绝」入口不同，
// 悬浮问号图标时展示。GitLab 的 /approve 是 MR 保留 quick action，
// issue 上必须用无斜杠的 orcsy approve / orcsy reject。
import { NTooltip } from 'naive-ui'
import { tNow as t } from '../lang'

// 亚克力提示框：半透明背景 + backdrop-filter 模糊，颜色由全局双主题变量驱动
const tipStyle = {
  maxWidth: '460px',
  background: 'var(--orcsy-tip-bg)',
  border: '1px solid var(--orcsy-tip-border)',
  borderRadius: '10px',
  boxShadow: 'var(--orcsy-tip-shadow)',
  backdropFilter: 'blur(16px) saturate(160%)',
  WebkitBackdropFilter: 'blur(16px) saturate(160%)',
}
const contentStyle = {
  color: 'var(--text)',
  padding: '10px 12px',
  fontSize: '13px',
}
</script>

<template>
  <NTooltip trigger="hover" placement="top" :style="tipStyle"
            :content-style="contentStyle" :show-arrow="false">
    <template #trigger>
      <i class="ms sm" style="cursor:help; color: var(--dim, #8891a5); font-size:16px;" tabindex="0">help_outline</i>
    </template>
    <div style="line-height:1.65">
      <div><b>{{ t('Provider 审批方式') }}</b></div>
      <div>• {{ t('飞书 / 钉钉：在厂商审批应用里点「同意」或「拒绝」；Orcsy 轮询审批实例。') }}</div>
      <div>• {{ t('GitHub：在审批 issue 里评论 /approve 或 /reject 原因。') }}</div>
      <div>• {{ t('GitLab：在审批 issue 里评论 orcsy approve 或 orcsy reject 原因（/approve 是 GitLab MR 保留命令，issue 上不可用）。') }}</div>
      <div style="opacity:.75; margin-top:6px">{{ t('白名单 email 是 quorum 计数口径；未登记账号的审批/评论不计入。') }}</div>
    </div>
  </NTooltip>
</template>

<style>
/* 双主题亚克力色：浅色=半透明白，深色=半透明深蓝；跟随 html[data-theme] */
:root[data-theme="light"] {
  --orcsy-tip-bg: rgba(255, 255, 255, 0.72);
  --orcsy-tip-border: rgba(203, 213, 234, 0.9);
  --orcsy-tip-shadow: 0 12px 32px rgba(29, 38, 55, 0.16);
}
:root[data-theme="dark"] {
  --orcsy-tip-bg: rgba(19, 26, 42, 0.72);
  --orcsy-tip-border: rgba(120, 150, 220, 0.28);
  --orcsy-tip-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
}
</style>
