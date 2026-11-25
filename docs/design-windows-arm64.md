# Windows ARM64 原生安装包与自更新

> 落地：2026-11-26　|　分支：`main`（随 0.35.0 批次）
> 关联：`build/build.py`、`build/installer.iss`、`orcsy/updater.py`、`.github/workflows/release.yml`

## 1. 背景：为什么过去只有 zip

`orcsy-web.exe` / `orcsy-tray.exe` 由 PyInstaller 打包，产物架构 = **构建机 Python 架构**。
PyInstaller 官方不支持交叉编译（“must install PyInstaller on each platform and bundle
your app separately on each”），而此前的构建机与 CI 都是 x64 Windows，因此只能产出
x64 exe。ARM64 用户当时有两条路：

1. 安装 amd64 安装包，在 ARM64 Win11 上通过 Prism 仿真运行；
2. 使用 `orcsy-{ver}-windows-arm64.zip` 源码便携包（要求本机 ARM64 Python/uv）。

## 2. 目标

- 发布原生 ARM64 首次安装包 `orcsy-setup-{ver}-windows-arm64.exe`；
- 发布 ARM64 自更新包 `orcsy-update-{ver}-windows-arm64.zip`；
- ARM64 用户升级时**绝不能**被替换成 x64 仿真版；
- x64 既有产物、命名与升级路径保持不变。

## 3. 更新器：按运行架构选包（最关键）

`orcsy/updater.py` 新增：

- `_host_update_arch()`：取 `platform.machine()`，即**当前运行进程**的架构
  （x64 构建在 ARM64 上仿真运行时仍是 `AMD64`，因此继续领 amd64 包）；
- `_pick_update_asset(assets, latest, arch=None)`：
  - `arm64`：必须命中 `*-windows-arm64.zip`；缺失时抛 `UpdateError` 并提示改用
    ARM64 安装包，**不回退** amd64；
  - `amd64`：排除 arm64 包；未标注架构的历史更新包视为 amd64（向后兼容旧 Release）。

命名约定：

| 架构 | 安装包 | 更新包 |
| --- | --- | --- |
| amd64 | `orcsy-setup-{ver}-windows-amd64.exe` | `orcsy-update-{ver}.zip`（保持历史名） |
| arm64 | `orcsy-setup-{ver}-windows-arm64.exe` | `orcsy-update-{ver}-windows-arm64.zip` |

## 4. 构建

`build/build.py`：

- `host_arch()` / `resolve_windows_arch()`：目标架构必须等于宿主架构，跨架构直接退出
  （PyInstaller 限制），错误信息给出 ARM64 宿主指引；
- `--arch {auto,amd64,arm64}`：默认 auto=宿主；`--skip-frontend` 供 CI 复用 x64 构建
  产出的 `dashboard_html.py`；
- `update_zip_name()` / `build_update_zip(ver, arch)`：按上表命名；
- arm64 构建时给 ISCC 追加 `/DArm64`（`#ifdef` 判定，避免预处理器字符串比较歧义）。

`build/installer.iss`：

- `/DArm64` 未定义 = amd64；输出名 `-windows-{#MyArchTag}`；
- `arm64`：`ArchitecturesAllowed=arm64`、`ArchitecturesInstallIn64BitMode=arm64`；
- `amd64`：保留 `ArchitecturesInstallIn64BitMode=x64compatible arm64`（允许 ARM64 仿真安装）。

## 5. CI（`.github/workflows/release.yml`）

- x64 job 额外上传 `dashboard-html` artifact（`orcsy/dashboard_html.py`）；
- 新增 `build-windows-arm64` job：
  - `runs-on: windows-11-arm`（GitHub 官方 ARM64 Windows runner）；
  - `uv python install 3.12` + `uv sync --dev --python 3.12 --managed-python`，
    并断言 `platform.machine()` 为 arm64（防止误用 x64 解释器）；
  - 下载 x64 job 的 `dashboard_html.py`，避免在 ARM64 上重复装 Node；
  - 安装 Inno Setup 6（x86 编译器，ARM64 上走仿真），`choco` 缺失时回退官方安装器；
  - `build.py --windows-only --arch arm64 --skip-frontend` → 校验 → 上传
    `windows-arm64-artifacts`；
- `prepublish-smoke` 依赖两个 Windows job，资产清单/数量核对/zip 成员 smoke 覆盖
  arm64 安装包与更新包；`release` job 的 `dist/orcsy-*` 通配自动带上新产物。

## 6. 依赖可用性（已验证）

ARM64 Windows + CPython 3.12 下均为二进制轮子或纯 Python：
Pillow（`win_arm64` 轮子）、PyYAML（`win_arm64`）、websockets（`py3-none-any` 回退）、
pystray/httpx/segno（纯 Python）、PyInstaller（`py3-none-win_arm64` 轮子）。

## 7. 验收

- 单测：`tests/test_updater.py` 新增 `_pick_update_asset` 三分支（arm64/amd64/旧包）；
  `tests/test_release.py` 新增 arm64 安装/更新包纳入清单 + 跨架构守卫；
- 发布门禁：`verify_release.py` 对 arm64 四件套（exe/zip + sha256）校验；
- 实机：ARM64 上安装原生包 → 检查「更新包按架构选择」与一次完整升级；
- 回滚：若 windows-11-arm runner 不可用，删除该 job 即可退回 zip 方案，x64 链路不受影响。

## 8. 已知限制

- 不能在 x64 上构建 arm64 产物（PyInstaller 限制），本地验证需 ARM64 Windows 机器/VM；
- ARM64 安装包与 amd64 共用 AppId：同机安装了 amd64 后装 arm64 会原地替换，属预期语义；
- 产物仍为未签名（个人开发者不接商业证书），SHA256 仅保证完整性，不证明发布者。
