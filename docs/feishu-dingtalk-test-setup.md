# 飞书 / 钉钉免费测试租户接入指南

> 适用版本：**0.36.0+**　|　更新：2026-11-27
> 目标：不购买商业版或试用套餐，用个人免费团队完成 Orcsy 风险审批的外部会签真机冒烟。
> 相关：[`design-risk-approval-phase2.md`](design-risk-approval-phase2.md)、
> [`用户指南.md`](用户指南.md) §10.23.2

Orcsy 的飞书/钉钉适配器当前使用**审批实例轮询**，不依赖 Webhook。因此本地开发
不需要公网域名或反向代理：`risk_approval.callback_url` 留空即可。SaaS 厂商无法
回调 `localhost`，这是预期行为，不是配置失败。

## 0. 先确认边界

- 免费团队/企业足够完成开发测试；是否需要企业认证、更高 API 配额或付费版，
  以厂商控制台的实际提示为准。
- 飞书官方自建应用文档明确允许“测试阶段自行创建一个新企业”，在新企业内建应用、
  加权限，管理员即开发者本人，权限基本免审。
- 钉钉个人可免费创建团队/组织；以该组织管理员身份创建**企业内部应用**即可。
  若控制台提供“测试企业/试用环境”入口，可直接使用；没有也不影响内部应用测试。
- 生产环境不要把测试租户凭据与真实审批流混用；`config.yaml` 中的
  `app_secret` / `app_key` 是明文，注意文件权限与备份范围。

## 1. 飞书接入步骤

### 1.1 创建测试企业

1. 用手机号注册飞书；
2. 创建一个免费团队/企业（名称可用测试名），自己为管理员；
3. 邀请至少 2 个测试成员（会签需要两个不同账号）。

参考：[创建企业](https://www.feishu.cn/hc/zh-CN/articles/360043741453-%E5%88%9B%E5%BB%BA%E4%BC%81%E4%B8%9A)

### 1.2 创建企业自建应用

1. 打开 [飞书开发者后台](https://open.feishu.cn/app)；
2. 创建**企业自建应用**，记录 `App ID` / `App Secret`；
3. 在权限管理中开通审批相关权限（搜索 `approval`），按控制台提示发布应用版本；
4. 因为你是测试企业管理员，审核可由自己立即通过。

官方流程：[自建应用开发流程](https://open.feishu.cn/document/home/introduction-to-custom-app-development/self-built-application-development-process)

### 1.3 创建最小审批模板

在飞书「审批」管理后台创建一个自定义审批：

- 表单只放一个**文本控件**，或所有字段均设为非必填；
- 审批流程设为**会签**，加入两个测试成员（不要用“或签”，否则第一人同意后
  流程即结束，第二人不会产生审批任务）；
- 记录 `approval_code`（审批定义编码）；
- 记录文本控件的 **widget id**，填入 Orcsy 的 `form_widget_id`；
- 若模板没有必填字段，`form_widget_id` 可留空，适配器会提交空表单 `{}`。

> `approval_code` 与 widget id 可从审批定义详情或开放平台接口获取；不要填审批
> 模板的名称或 URL。

### 1.4 获取用户 ID

- `originator_user_id`：发起审批的飞书 **user_id**（不是 open_id）；
- `approvers` 中每个 email 对应的飞书身份：建议统一使用 **user_id**；
  适配器轮询 `task_list` 时读取 `user_id`（兼容 `open_id`），但创建实例必须用
  user_id。

从管理后台成员详情或通讯录接口获取 user_id。

### 1.5 Orcsy 配置示例

```yaml
approval_providers:
  feishu-test:
    type: feishu
    app_id: cli_xxxxxxxx
    app_secret: xxxxxxxx
    approval_code: XXXXXXXXXXXXXXXX
    originator_user_id: user_id_xxx    # 飞书 user_id（不是 ou_ 开头的 open_id）
    form_widget_id: widgetxxxxxxxx    # 可选，见 1.3
    callback_secret: ""               # 轮询模式可留空

approvers:
  alice@example.com:
    feishu: user_id_alice
  bob@example.com:
    feishu: user_id_bob

risk_approval:
  enabled: true
  policy:
    low: off
    medium: off
    high: quorum
  provider: feishu-test
  quorum:
    high: 2
  allowed_approvers:
    high: [alice@example.com, bob@example.com]
  callback_url: ""                    # 本地测试留空 = 仅轮询
  poll_interval_seconds: 5
  timeout_minutes: 60
```

## 2. 钉钉接入步骤

### 2.1 创建免费团队/组织

1. 注册钉钉；
2. 创建一个免费团队/组织，自己为管理员；
3. 在通讯录中加入至少 2 个测试成员。

### 2.2 创建企业内部应用

1. 打开 [钉钉开放平台](https://open.dingtalk.com/)；
2. 创建**企业内部应用**（不是第三方 ISV 应用）；
3. 记录 `AppKey` / `AppSecret`；
4. 开通审批相关接口权限，发布应用版本；组织管理员审核（即你自己）。

### 2.3 创建最小审批模板

在钉钉「OA 审批」后台创建模板：

- **不要放必填表单控件**：Orcsy 当前创建实例时提交
  `form_component_values: []`，模板有必填项会被厂商拒绝；
- 审批流程设为**会签**，加入两个测试成员；
- 记录模板的 `process_code`（通常形如 `PROC-XXXXXXXX`）。

`process_code` 可从模板详情获取，或调用钉钉 `topapi/process/get_by_name`
按模板名称查询。

### 2.4 获取用户与部门 ID

- `originator_user_id`：发起审批的钉钉 **userid**；
- `dept_id`：发起人所在部门 ID；Orcsy 配置默认 `-1` 表示根部门，建议填写
  通讯录中的真实部门 ID；
- `approvers` 中每个 email 对应审批人的 **userid**。

### 2.5 Orcsy 配置示例

```yaml
approval_providers:
  dingtalk-test:
    type: dingtalk
    app_key: dingxxxxxxxx
    app_secret: xxxxxxxx
    process_code: PROC-XXXXXXXX
    originator_user_id: manager1234
    dept_id: 1                        # 以实际通讯录部门 ID 为准
    callback_secret: ""               # 轮询模式可留空

approvers:
  alice@example.com:
    dingtalk: userid_alice
  bob@example.com:
    dingtalk: userid_bob

risk_approval:
  enabled: true
  policy:
    low: off
    medium: off
    high: quorum
  provider: dingtalk-test
  quorum:
    high: 2
  allowed_approvers:
    high: [alice@example.com, bob@example.com]
  callback_url: ""                    # 本地测试留空 = 仅轮询
  poll_interval_seconds: 5
  timeout_minutes: 60
```

## 3. 会签真机冒烟清单

准备一个高风险改动任务（例如修改 `orcsy/updater.py`），触发合入前风险审批：

- [ ] 看板待审批卡显示 Provider、`0/2` 进度与两位审批人 email；
- [ ] 第一位审批人批准后变为 `1/2`，任务仍未合入；
- [ ] 第二位审批人批准后变为 `2/2`，任务继续 premerge/合入；
- [ ] 任一白名单审批人拒绝 → 一票否决，任务阻断并保留分支；
- [ ] 未登记账号批准/拒绝 → 被忽略并写审计；
- [ ] 超时 → `risk_approval_timeout`，任务阻断；
- [ ] 删除调用或重启后，pending 请求按持久化记录恢复等待。

> 审批人在厂商界面操作后，Orcsy 最多等待一个 `poll_interval_seconds` 周期才
> 看到结果；本地测试建议设为 5 秒。

## 4. 常见问题

| 现象 | 常见原因 | 处理 |
| --- | --- | --- |
| `invalid app_id` / `invalid app_secret` | 凭据不属于当前租户，或复制了别的应用 | 在开发者后台重新核对 App ID/Secret |
| 飞书创建实例报 `approval_code` 无效 | 模板未发布/编码错误 | 使用审批定义编码，不是模板名称 |
| 飞书报表单错误 | 模板存在必填字段但未提供 `form_widget_id` | 去掉必填项，或配置正确的文本控件 ID |
| 钉钉创建实例报表单为空 | 模板含必填组件 | 改为无必填组件的测试模板 |
| 审批后 `task_list` / `operation_records` 为空 | 流程是“或签”，或审批人不在流程中 | 改为会签并加入两位测试成员 |
| 有审批记录但进度不增加 | `approvers` 中 user_id 与厂商返回身份不一致 | 用实际 user_id 建立 email 映射 |
| 权限不足/403 | 应用权限未开通或版本未发布 | 补权限并重新发布应用 |
| 回调一直不通 | 本地 `callback_url` 指向 localhost | 本地留空走轮询；公网再配 HTTPS 回调 |

## 5. 与 mock 测试的分工

仓库内 `tests/mock_vendor_server.py` + `tests/test_approval_adapters_http.py`
已覆盖四类 Provider 的 HTTP 契约、会签计数、一票否决、Webhook 验签与持久化，
可在 CI/离线环境反复运行。

本指南用于**真实厂商租户的最后一轮冒烟**，重点验证：

- 真实应用的权限与发布流程；
- 真实审批模板的 `approval_code` / `process_code`、表单字段与会签流程；
- 真实成员身份到 email 的映射；
- 真实租户下的 API 配额、错误码与网络可达性。

本地 mock 通过不代表真实租户一定通过；反之真实租户发现问题时，优先用 mock
复现协议层问题，再判断是适配器缺陷还是厂商配置差异。
