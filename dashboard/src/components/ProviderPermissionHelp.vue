<script setup lang="ts">
// Provider 授权权限提示：各厂商后台需要开通的 scope 不同，
// 悬浮问号时列出最小权限，方便用户去企业后台自主授权。
import { NTooltip } from 'naive-ui'
import { tNow as t } from '../lang'

const tipStyle = { maxWidth: '540px' }
const contentStyle = { padding: '10px 12px', fontSize: '12.5px' }
</script>

<template>
  <NTooltip trigger="hover" placement="top" :style="tipStyle"
            :content-style="contentStyle" :show-arrow="false">
    <template #trigger>
      <i class="ms sm" style="cursor:help; color: var(--dim, #8891a5); font-size:16px;" tabindex="0">help_outline</i>
    </template>
    <div style="line-height:1.65">
      <div><b>{{ t('各 Provider 需要开通的权限') }}</b></div>
      <div>• <b>GitLab</b>：{{ t('细粒度 token 勾选 Work Item: Create / Read（可选 Member: Read）；经典 token 勾选 api。账号需是项目成员。') }}</div>
      <div>• <b>GitHub</b>：{{ t('细粒度 token 勾选 Issues: Read and write（Metadata 自动只读）；经典 token 勾选 repo / public_repo。审批人需能评论 issue。') }}</div>
      <div>• <b>飞书</b>：{{ t('自建应用开通 approval:approval（创建/查询审批实例）；可选 contact:contact.base:readonly、contact:user.email:readonly 用于查 user_id。发布应用版本。免费版读不到邮箱时，白名单 email 可用占位值再映射 user_id。') }}</div>
      <div>• <b>钉钉</b>：{{ t('企业内部应用开通审批实例创建/读取权限；可选通讯录读取权限。发布应用版本。') }}</div>
      <div style="opacity:.75; margin-top:6px">{{ t('轮询模式不需要回调权限；callback_secret 仅 Webhook 需要。') }}</div>
    </div>
  </NTooltip>
</template>
