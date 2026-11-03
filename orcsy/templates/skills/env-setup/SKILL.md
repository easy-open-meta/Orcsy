---
name: env-setup
description: 项目依赖的语言/工具链缺失时的环境引导：预检诊断 → 镜像源测速选源 → 幂等安装（包管理器优先，无代理时国内镜像直下兜底）→ 会话内修复 PATH/变量 → 验证后继续任务。
scope: global
enabled: true
origin: created
---

# 环境引导：缺失工具链的预检与修复

当 `run_command` 返回「命令 X 不存在（未安装或不在 PATH）」时按本技能处理。
流程是**预检 → 修复 → 验证**的闭环：每完成一个阶段都要重新验证，根据最新
结果决定下一步，而不是按预设清单从头跑到尾。

## 阶段一：预检（先诊断，再动手）

按顺序收集事实，**别跳过预检直接安装**——很多「找不到命令」其实装了：

```powershell
# Windows（按需替换 <命令>）
where <命令>
Get-Command <命令> -ErrorAction SilentlyContinue
```
```bash
# macOS / Linux
command -v <命令> || true
```

预检判定表：

| 结果 | 结论 | 动作 |
|---|---|---|
| 能找到 | 已安装 | 直接用；找不到说明是调用方式问题，检查拼写/版本后缀 |
| 找不到，但常见安装目录里有（用 dir/ls 查 Program Files、%LOCALAPPDATA%\Programs、~\scoop\shims、/usr/local/bin 等） | 装了不在 PATH | **不要重装**：用绝对路径调用（白名单按文件名匹配，`go.exe` ≡ `go`），或按阶段三注册用户级 PATH |
| 确实没装 | 需安装 | 进入阶段二 |

**probe-before-install**：二进制已在磁盘上时只需做「注册 PATH」一步，
重新下载几百 MB 是纯浪费。

## 阶段二A：用系统包管理器安装（网络通畅时首选，幂等）

按平台优先级选择**已可用**的包管理器（先 `where` 确认哪个存在）：

| 平台 | 包管理器 | 示例 |
|---|---|---|
| Windows | winget（首选）| `winget install --id GoLang.Go -e --accept-source-agreements --accept-package-agreements` |
| Windows | scoop | `scoop install go` |
| Windows | choco | `choco install golang -y` |
| macOS | brew | `brew list go >/dev/null 2>&1 \|\| brew install go` |
| Linux (deb) | apt | `apt-get install -y golang` |
| Linux (rpm) | dnf | `dnf install -y golang` |
| 通用 | 官方渠道 | Node 用 nvm、Python 用 uv，参考官方文档 |

**winget 与官方源多为境外 CDN，国内无代理网络常超时**。安装输出里出现
网络类报错（`timeout`、`connection reset`、`0x80072ee2` 等）时不要重试，
直接转阶段二B 镜像直下。

## 阶段二B：无代理时的国内镜像直下（包管理器失败/不可用时自动切换）

国内镜像均为**无需任何代理即可直连**的境内 CDN。先用 3 秒探测决定走哪条路
（PowerShell 一律用 `powershell -NoProfile -Command` 执行）：

```powershell
try { Invoke-WebRequest -Uri "https://go.dev" -TimeoutSec 3 -UseBasicParsing | Out-Null; "官方源可达" }
catch { "官方源不可达 → 用镜像直下" }
```

**选源优先级**：正文末尾若有**「镜像源测速优选」动态段**（系统实测延迟后
追加），按它的顺序用第一个源；没有该段时可先调用 `speedtest_mirrors` 工具
（可只测单类别，如 `{"category": "go_dist"}`）拿到最快排序——结果会同步到
看板；两者都没有再按下表默认顺序。

本节的下载/安装命令**同样受访问权限审批约束**：变更前询问/自主模式下会
挂起等用户批准（审批框里能看到完整命令，用户据此知道下载源与落盘位置），
批准后才开始下载；被拒绝则 finish（action=blocked）说明缺环境。

### 通用流程（zip 解压式：零管理员、零代理、幂等）

1. 从镜像下载 zip：`Invoke-WebRequest -OutFile`，run_command 给足
   `timeout_seconds: 600`；先设 `$ProgressPreference='SilentlyContinue'`，
   否则下载会明显变慢；
2. 解压到**用户可写目录** `%LOCALAPPDATA%\Programs`
   （`Expand-Archive -Force`，无需管理员权限；不要解压到 Program Files）；
3. **装完立即注册用户级 PATH，别拖到收尾**：
   `[Environment]::SetEnvironmentVariable("PATH", "$userPath;$bin", "User")`
   （避免 `setx`——它有 1024 字符截断陷阱）；
4. PATH 注册只影响新启动的进程；当前会话用**绝对路径**验证。
   run_command 每次会现读最新用户 PATH，下一条命令起裸名即可用；
5. 遇必须管理员权限的安装器（msi/exe）不要自作主张，finish
   （action=blocked）在 notes 里请用户手动完成后重跑任务。

### 示例：安装 Go（完整流程，可直接执行）

```powershell
$ProgressPreference = 'SilentlyContinue'
$Arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
# golang.google.cn 是 Go 官方中国镜像，无需代理直连
$Ver = ((Invoke-WebRequest "https://golang.google.cn/VERSION?m=text" -UseBasicParsing -TimeoutSec 15).Content -split "`n")[0].Trim()
$Zip = "$env:TEMP\$Ver.windows-$Arch.zip"
Invoke-WebRequest "https://golang.google.cn/dl/$Ver.windows-$Arch.zip" -OutFile $Zip -UseBasicParsing
Expand-Archive $Zip "$env:LOCALAPPDATA\Programs" -Force          # 得到 ...\Programs\go
$GoBin = "$env:LOCALAPPDATA\Programs\go\bin"
$UserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($UserPath -notlike "*$GoBin*") { [Environment]::SetEnvironmentVariable("PATH", "$UserPath;$GoBin", "User") }
if (-not [Environment]::GetEnvironmentVariable("GOPROXY", "User")) {
  [Environment]::SetEnvironmentVariable("GOPROXY", "https://goproxy.cn,direct", "User")  # 模块拉取也走国内
}
& "$GoBin\go.exe" version                                        # 当前会话验证
```

下载失败换备用镜像（文件名相同）：`https://mirrors.aliyun.com/golang/`。
（USTC/华为云镜像有防盗链或校园网限制，实测不稳定，不要用。）
macOS/Linux 等价做法：从镜像下 `*.darwin-arm64.tar.gz` / `*.linux-amd64.tar.gz`
解压到 `~/.local/go`，把 `~/.local/go/bin` 加进 PATH（修改 `~/.profile` /
`~/.zshrc` 前先征得用户同意）。

### 常见运行时的镜像直下源（无需代理）

| 软件 | 镜像直下 | 备注 |
|---|---|---|
| Go | https://golang.google.cn/dl/ | 官方中国镜像；备选 mirrors.aliyun.com/golang/ |
| Node.js | https://npmmirror.com/mirrors/node/ | 按版本/架构选 zip，解压即用 |
| Python | https://mirrors.huaweicloud.com/python/ | 备选 npmmirror.com/mirrors/python/ |
| JDK（Temurin）| https://mirrors.tuna.tsinghua.edu.cn/Adoptium/ | zip 解压后把 `JAVA_HOME` 写进 env.vars |
| Go 模块 | `GOPROXY=https://goproxy.cn,direct` | 一次配置永久生效 |
| npm 包 | `npm config set registry https://registry.npmmirror.com` | 或每次带 `--registry` |
| pip 包 | `-i https://pypi.tuna.tsinghua.edu.cn/simple` | 或 env.vars 的 `PIP_INDEX_URL` |

安装约束（二A/二B 都必须遵守）：

- **幂等**：先检测再安装（如 `brew list X || brew install X`），已装不重装；
- **只装任务明确需要的工具链**，不顺手升级/安装无关软件；只用官方包名与
  上表镜像源，不从其它站点下载可执行文件；
- 安装耗时可能 10~30 分钟；大文件下载超时时用**更长超时重试同一条命令**，
  不要同时并发跑第二个安装进程；
- 全部下载路径都失败（镜像也不可达）→ finish（action=blocked），notes 写明
  试过的镜像与软件版本，请用户配置代理（env.vars 的 `HTTP_PROXY`）或手动
  安装后重跑。

## 阶段三：会话内修复（装完仍找不到时）

安装成功但裸名命令仍不可用，按顺序尝试：

1. **直接重试裸名**——run_command 每次现读注册表最新用户 PATH，并到常见
   安装目录解析（Program Files / WinGet Links / scoop shims /
   %LOCALAPPDATA%\Programs 等），装完通常立即可用，无需重启看板；
2. **绝对路径调用**：如 `"%LOCALAPPDATA%\Programs\go\bin\go.exe" build ./...`
   （白名单按文件名匹配，绝对路径放行）；
3. **工具链约定变量**：sdkmanager/gradle 等按 `JAVA_HOME`/`ANDROID_HOME`
   找依赖工具链时，把这些变量加进看板设置页「Agent 子进程环境变量」
   （即 config.yaml 的 `env.vars`，例如
   `JAVA_HOME=C:\Program Files\Eclipse Adoptium\jdk-17`），保存后重试；
4. 都不行：finish（action=blocked），在 notes 写清**已完成的安装**、
   **建议用户做的动作**（加 PATH / 配 env.vars 的具体键值）、
   **重跑本任务即可继续**。

## 验证与收尾

修复动作完成后必须验证再回任务：

1. `<命令> version` 或项目自身的最小验证命令（如 `go build ./...`）通过；
2. 通过 → 继续原任务；未通过 → 回到阶段一重新预检（最多一轮，别死循环）；
3. 全程在任务「执行进度」里留痕（装了什么、版本、验证结果），任务中断重跑
   时不用重新诊断。

## 依赖拉取失败（网络问题）

`go mod download` / `npm install` / `pip install` 出现 `unexpected EOF`、
`i/o timeout`、`TLS handshake timeout`、`connection reset` 等，通常是访问
境外源不稳，**不是代码问题**：

- 先按阶段二B 的 3 秒探测确认官方源不可达，再**配置镜像后重试一次**
  （看板设置页「Agent 子进程环境变量」或 config.yaml 的 `env.vars`）：
  - Go：`GOPROXY=https://goproxy.cn,direct`
  - Node：`HTTP_PROXY` 指向本机代理，或 `npm config set registry https://registry.npmmirror.com`
  - Python：`PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`
- 配置属全局设置，你无法自改时 finish（action=blocked）并明确写出建议的
  `env.vars` 键值，让用户改完重跑；
- 不要连续重试同一条下载命令超过 2 次——重试只会重复消耗轮次与时间。
