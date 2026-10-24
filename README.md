# 微信划线开关

关闭 / 还原 Windows 微信公众号文章里的「划线评论」绿色虚线及点击面板。带图形界面，单文件 exe，无需安装 Python。

**[下载最新版](https://github.com/xxhhlk/wx-underline-switch/releases/latest)** · `WxUnderlineSwitch.exe`

> **v1.2.0 是版本锁定的离线组合版。** 同时处理划线开关、组件校验初始化和主程序的一处摘要比较，并同步已识别的解压副本。无需常驻内存注入。只支持下列文件基线，不支持的文件会拒绝写入；不再对任意版本只改划线开关。

## 支持范围

本机验证环境：**Windows / Sandboxie，微信 4.1.15.56，RadiumWMPF 25799**。主程序基线已有其他本机修改，因此相同版本号的官方 DLL 也可能不在白名单内。工具严格检查整个文件 SHA1，不按版本号或几个指令字节放行，不提供强制跳过校验选项。

| 文件 | 支持的输入 SHA1 |
|---|---|
| 原始组件包 | `df6e79f846c818ea5c625da09e434fef5f0dead6` |
| v1.0.x 基础去划线组件包 | `fa514c5befc8c8fa9cb7f088ecfb5fe9a72e38d0` |
| 完整组合组件包 | `c769afeec1325cf0fc497d1e0d6892c8f023f1b7` |
| 主程序本机基线 | `2f43ada7d9119288c29ac0f262a034addc5ea520` |
| 主程序完整组合 | `22b3c7e74fa99203885af5eec2a51980b8917a15` |

组件包和 `Weixin.dll` 必须在同一目录。目标、runtime、备份目录及其父目录不支持符号链接或重解析点（含目录 junction），多硬链接目标也会被拒绝。工具只分发补丁逻辑，**不分发腾讯 DLL 或组件包**。普通安装可定位当前 Windows 用户的缓存；Sandboxie 使用所选组件包对应沙盒的 `user/current` 缓存，不会退回宿主缓存。不符合预期的布局应使用 CLI 明确指定对应 runtime。目标、runtime、事务备份路径及其父目录不支持符号链接或重解析点（含目录 junction），遇到会拒绝操作，应使用普通目录。

## 使用方法

1. 完全退出微信及其内置浏览器，确认 `Weixin.exe`、`WeChat.exe`、`WeChatAppEx.exe` 全部退出。
2. 启动工具，检查选中的 `RadiumWMPF.bin` 及检测日志中的主程序、缓存路径。
3. 点「关闭划线」，确认组合修改。工具先完整备份并校验，再写入组件包、主程序及已有解压副本。
4. 成功后手动启动微信，打开公众号文章检查效果与网页功能。

**当前已安装完整组合且正常运行时，不必重新应用。** 运行中只检测状态可以正常进行；写入和还原会拒绝继续。遇到权限不足应先核对目标，必要时以管理员身份运行工具。

### 命令行

```bat
WxUnderlineSwitch.exe --cli status
WxUnderlineSwitch.exe --cli apply
WxUnderlineSwitch.exe --cli revert
```

`apply` 应用完整组合，`revert` 撤销完整组合。可追加：

```bat
WxUnderlineSwitch.exe --cli status --target "D:\Weixin\4.1.15.56\RadiumWMPF.bin"
WxUnderlineSwitch.exe --cli apply --target "D:\Weixin\4.1.15.56\RadiumWMPF.bin" --runtime "D:\cache\25799\extracted\runtime"
```

`--runtime` 指向与选中组件包对应的 `extracted/runtime` 目录，仅用于 CLI。未知文件、异常缓存、未能确认进程已退出时，工具拒绝写入。

### 状态含义

| 状态 | 含义 |
|---|---|
| `original` | 已识别的原始组件包 / 主程序本机基线 |
| `basic` | 旧版基础去划线状态，尚未应用完整组合 |
| `combo` | 已应用对应组合部分 |
| `missing` | 对应 runtime 尚不存在；不会创建缓存目录，等待微信自行解压 |
| `unknown` 或错误信息 | 文件或路径不支持，不能写入 |

组件包、主程序和缓存分别检测；仅看到划线关闭不能说明完整组合已生效。已有 runtime 的关键资源缺失或不可读时，工具会拒绝操作，不自动删除资源目录。

## 备份与完整还原

v1.2.0 在修改前把**每个将写入的文件完整备份**，备份及候选副本均回读校验 SHA256。事务记录默认位于：

```
%LOCALAPPDATA%\WxUnderlineSwitch\transactions\<事务编号>\journal.json
```

配置中的旧版 `backups` 只有摘要和偏移，不能代替新版完整文件备份。

写入前再次检查进程和目标摘要；每个目标通过同目录暂存文件替换，写后回读。捕获到错误时，按相反顺序恢复已写文件；若目标已变成未知第三种内容，则拒绝强行覆盖并报告备份位置。跨多个文件的修改无法成为一次文件系统原子操作，断电或进程被强杀时仍可能留下中间状态。未完成事务会阻止后续修改，需按日志核对并恢复备份后再处理。

新版「还原」会：

- 把组件包的划线开关恢复 `true`，撤销初始化 NOP，并重算 ZIP CRC，核对原始组件包摘要。
- 撤销主程序定向摘要比较，恢复上述**本机基线**，保留该基线已有的其他修改。
- 把已有且受支持的 `runtime/flue.dll` 同步为原始组件条目。

它不会还原主程序基线中其他人的修改，也不会递归清理 `extracted` 或删除 ICU / pak 资源。备份保留，工具不自动清理。

**不要使用 v1.0.x 的 GUI / CLI 还原已安装的完整组合。** 旧版只处理划线字面量和 CRC，会让完整组合变成不匹配状态。早期 `scripts/weixin_gate_install.py --restore --apply` 也仅撤销主程序部分，不是完整回退；正常用户请使用新版「还原」。

## 本机验证记录

2026-10-24，安装同一组合后：

| 项目 | 结果 |
|---|---|
| 用户反馈 | 连续约 8 小时无问题，内置浏览器正常 |
| 健康日志 | 07:20:50–16:26:52，9 小时 6 分 2 秒，无基线后资源失效事件 |
| 结束后复核 | ICU、flue、三项资源 pak、中文语言包六项均可打开 |
| 补丁离线验证 | 实际候选机器码 152 组仿真通过；主程序回退文件与安装前摘要一致 |
| 后台状态 | 用户要求收尾后已停临时只读监测，无常驻内存注入 |

原计划观察 24 小时，在用户要求收尾后提前结束，**不记为完整 24 小时通过**。这证明本机该版本组合通过长时间使用验证，不保证所有环境或永久稳定；组合各部分的独立贡献尚未拆分验证。

本机取证（资源监测日志、离线仿真输出、安装回执）不随发布包上传。新版事务测试在临时副本上完成，不改动正在正常使用的微信安装。

## 原理

### 划线开关

公众号文章在微信内置浏览器 RadiumWMPF 中运行，运行时原生注入：

```js
WeixinJSBridge.support_mp_underline = true;
```

文章模块初始化时读取此标志。Windows 侧把它设为假值可关闭划线评论能力；修改页面 DOM / CSS 无法可靠关闭该能力。组件包中将 UTF-16LE 的 `true` 等长替换为 `null`，各 8 字节。`false` 是 5 字符，会破坏等长约束，不能使用。

### 组件完整性校验

本机 `RadiumWMPF.bin` 是含 46 个 stored 条目的 ZIP，开关落在 `runtime/flue.dll` 中。修改后需区分三层信息：

| 层 | 修改后的状态 | 处理方式 |
|---|---|---|
| ZIP CRC32 | 条目数据改变后失配 | 重算本地头与中央目录两处 CRC |
| 清单 MD5 与签名 | flue 实际摘要不再等于签名清单 | 保留清单原文，只对指定摘要对作定向比较处理 |
| flue Authenticode | 签名覆盖数据改变后摘要失配 | 不恢复官方签名；调整组件校验初始化调用 |

无法用腾讯私钥重新签名，与能否通过其他文件修改保持本机功能正常，是两个不同问题。早期“改文件无法消除弹窗”的结论已由本机后续验证纠正。

### 网页故障链与组合修改

仅跳过 `ComponentVerification::Initialize` 后，用户实测弹窗消失，但网页仍会故障。动态捕获显示新网页进程在 `flue.dll+0x14bfd8f` 的 ICU 初始化失败路径断点退出，`icudtl.dat` 同时处于 `STATUS_DELETE_PENDING`；旧进程仍持有文件，新进程无法打开。其他 pak 也出现待删除。

ETW 观察到微信组件线程以包含 `FILE_DELETE_ON_CLOSE` 的选项批量打开资源。这支持组件清理参与故障链，但没有逐请求关联全部完成状态，也未捕获当次清理的完整校验调用栈；不能仅凭签名失配或 `WinVerifyTrust` 导入断言唯一根因。

最终组合包含：

1. `true → null` 及 ZIP CRC 修复。
2. flue RVA `0x25f5ca8` 的初始化调用改为 5 个 NOP。
3. Weixin RVA `0xb3cc29` 的单个摘要比较调用转向已有代码填充区，只接受实际摘要 `966e57b3d3af82572918113c43ce185d` / 清单摘要 `b256f0a03bf1bc8613af8e860b62717c` 这一对；其他输入回原比较函数。

直接修改清单 MD5 的实验使内置浏览器不可用，已撤销；新版保留签名清单原文。不禁用全局删除，不跳过整个主程序组件验证。官方数字签名仍不恢复。

## 微信更新后怎么办

更新可能覆盖组件包、主程序或两者。新版遇到未知摘要会拒绝修改，**不要套用旧 DLL、偏移或强行绕过白名单**。需要对新版重新定位、制作离线候选并验证。自动升级后的恢复能力不在当前版本保证范围内。

## 构建与测试

源码启动需 Python 3.8+ 并带 tkinter；构建推荐 Python 3.12，依赖见 `requirements-build.txt`。构建脚本兼容 Windows PowerShell 5.1，正文仅 ASCII。

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

可通过 `-PythonPath` 指定带 tkinter 的 Python，通过 `-BuildEnv` 指定隔离构建环境。默认位于 `%USERPROFILE%\.workbuddy\binaries\python\envs\wx-release312`。脚本依次安装依赖、静态检查、事务测试、GUI 桩件冒烟、生成图标、打包，并生成英文 / 中文名 exe。

```bat
python scripts\combo_test.py
python scripts\gui_smoketest.py
python scripts\zip_crc_test.py
```

事务测试涵盖运行中拒绝、未知基线拒绝、写入失败回滚等边界。真实文件测试使用工作区副本，不包含在公开仓库；GUI 冒烟以桩件替换 tkinter，不弹窗口。

## 项目结构

```
src/wx_underline_switch.py   GUI / CLI、目标定位、离线扫描和 CRC helpers
src/offline_combo.py         严格白名单、组合变换、缓存同步与事务回滚
scripts/combo_test.py        组合事务回归测试
scripts/gui_smoketest.py     GUI 桩件冒烟
scripts/zip_crc_test.py      ZIP CRC 离线自检
build.ps1                   单文件 exe 构建
scripts/recon/              早期只读侦察工具
```

## 已知限制

- 仅 Windows；iOS / Android 使用独立通路，本工具不能关闭移动端划线。
- 关闭的是整套划线评论能力，点击讨论面板也会失效。
- 不提供通用微信版本支持或自动更新补丁。
- 不删除异常资源目录；已有缓存损坏时需按备份和日志另行处理。
- Tk 8.6 的 per-monitor 动态 DPI 切换有限，系统缩放改变后需重启工具。

## License

MIT
