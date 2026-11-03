---
name: env-setup
description: 项目依赖的语言/工具链缺失时的环境引导：探测包管理器并用它静默安装缺失工具，装完验证后继续任务。
scope: global
enabled: true
origin: created
---

# 环境引导：缺失工具链的安装

当 `run_command` 返回「命令 X 不存在（未安装或不在 PATH）」时，按本技能处理。

## 第一步：确认是真的没装

1. 用 `where <命令>` 确认（Windows）或 `which <命令>`；
2. 也检查常见安装路径是否在 PATH 之外（如 `%LOCALAPPDATA%\Programs`、`/usr/local/bin`）；
3. **PATH 之外的现成安装**：不要重复安装，在后续 run_command 里用绝对路径调用，或建议用户把它加入 PATH。

## 第二步：用系统包管理器静默安装

按平台优先级选择**已可用**的包管理器（用 `where` 先确认哪个存在）：

| 平台 | 包管理器 | 示例 |
|---|---|---|
| Windows | winget（首选）| `winget install --id GoLang.Go -e --accept-source-agreements --accept-package-agreements` |
| Windows | scoop | `scoop install go` |
| Windows | choco | `choco install golang -y` |
| macOS | brew | `brew install go` |
| Linux (deb) | apt | `sudo apt-get install -y golang` |
| Linux (rpm) | dnf | `sudo dnf install -y golang` |
| 通用 | 官方渠道 | Node 用 nvm、Python 用 uv，参考官方文档 |

安装约束（必须遵守）：

- **只装任务明确需要的工具链**，不要顺手升级/安装无关软件；
- 只用官方包名与官方源，不加第三方源；
- winget/choco/scoop 等安装命令属于**敏感操作**：在「自主模式 / 变更前询问」下会挂起等待用户批准，耐心等待即可，被拒绝则直接 finish（action=blocked）说明缺环境；
- 安装耗时可能超过 2 分钟，给足 `timeout_seconds`（如 600）。

## 第三步：验证并继续

1. `where <命令>` 或 `<命令> version` 验证可用；
2. **注意**：安装后新命令通常立即可用（子进程继承更新后的系统 PATH）；若仍找不到，改用绝对路径调用并记录在任务进度里；
3. 继续原任务。若包管理器也不可用且无法静默安装，finish（action=blocked）并在 notes 里写清「需要人工安装 <工具> 及建议命令」。
