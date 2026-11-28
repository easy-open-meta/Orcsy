<script setup lang="ts">
// 审批 Provider 操作差异提示：四家厂商的「批准/拒绝」入口不同，
// 悬浮问号图标时展示。GitLab 的 /approve 是 MR 保留 quick action，
// issue 上必须用无斜杠的 orcsy approve / orcsy reject。
import { NTooltip } from 'naive-ui'
import { tNow as t } from '../lang'
</script>

<template>
  <NTooltip trigger="hover" placement="top" :style="{ maxWidth: '460px' }">
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
