# 微信划线开关

关闭 / 还原微信 PC 版公众号文章里的「划线评论」绿色虚线标记。

划线评论是微信公众号的互动功能：读者选中某段文字发表评论后，该段文字下方会显示一条微信绿虚线，其他读者点它就能看到相关讨论。这个工具在客户端层面把这套能力关掉。

带图形界面，单文件 exe，绿色免安装。

**[下载最新版](https://github.com/xxhhlk/wx-underline-switch/releases/latest)** — `WxUnderlineSwitch.exe`，11.2 MB，无需安装运行时。

> **使用前必读**：`RadiumWMPF.bin` 里的 `flue.dll` 带腾讯 Authenticode 签名，改文件必然破坏它。微信会因此周期性弹出「微信遇到问题，将影响网页正常浏览，可重启微信后恢复」，重启即恢复、补丁仍然有效。这个提示无法消除（签名私钥只有腾讯持有）。详见[组件完整性校验](#组件完整性校验)。

---

## 效果

| 状态 | 文章正文 |
|---|---|
| 关闭前 | 句子下方有微信绿（`#07C160`）虚线，点击弹出划线评论面板 |
| 关闭后 | 无虚线、无点击响应，正文其余排版完全不变 |

---

## 原理

### 为什么不能从网页层改

公众号文章跑在微信内置浏览器（RadiumWMPF，基于 Chromium）里。以下方案实测全部无效：

| 方案 | 结果 |
|---|---|
| 注入 CSS（`text-decoration` / `border-bottom` / `background-image` / `box-shadow`） | 无效 |
| 注入 JS 遍历删除 `[class*=underline]` 元素 | 无效，删光后虚线仍在 |
| 拦截 `/mp/underline?action=get_appmsg_segment` 接口 | 无效，该接口从未被请求 |
| 修改页面 HTML 响应（hosts + 本地 MITM） | 只能影响首次加载，且对虚线无效 |

逐一排除后确认：**虚线不由网页 DOM 渲染**。全文档扫描（含 SVG、canvas、iframe、shadow DOM、伪元素）在句子所在区域找不到任何画线元素。

### 真正的开关

`RadiumWMPF.bin` 里内嵌一段**原生注入到页面的 JavaScript**：

```js
WeixinJSBridge.support_mp_underline = true;
```

页面侧的 `underline_action.js` 用它判断是否启用划线能力：

```js
ne = (window.WeixinJSBridge?.support_mp_underline)
     && (isWindows && cpVersion("3.9.6") || os.Mac || os.unifiedPC)
se = android(>=28001F3E) || ios(>=8.0.32) || ne
re = harmony || android || ios || ne || isMpapp
```

Windows 桌面端 `android` / `ios` / `harmony` 恒为 `false`，所以 `se` 和 `re` **就等于 `ne`**。把这条 flag 改成假值，Windows 侧整条特性链即断开。

在页面里用 JS 改这条 flag 不可靠——`underline_action.js` 在模块初始化时读取，存在时序竞争。改二进制则没有竞争。

---

## 使用方法

> 从 [Releases](https://github.com/xxhhlk/wx-underline-switch/releases/latest) 下载 `WxUnderlineSwitch.exe`，或自行构建（见[构建](#构建)一节，`build.ps1` 一条命令，会同时生成中文名副本 `微信划线开关.exe`），也可以直接 `python src\wx_underline_switch.py` 运行（需 Python 3.8+ 带 tkinter）。

1. **完全退出微信**（托盘图标右键退出，确认任务管理器里 `Weixin.exe` 和 `WeChatAppEx.exe` 都消失）
2. 双击 `WxUnderlineSwitch.exe`
3. 点「关闭划线」
4. 启动微信生效

工具会自动定位 `RadiumWMPF.bin`，覆盖普通安装目录和 Sandboxie 沙盒布局。识别不到时可用「浏览」手动指定。

用后微信会周期性弹出「微信遇到问题，将影响网页正常浏览，可重启微信后恢复」——这是文件签名被改动后的正常反应，点「暂不处理」或重启即可，补丁不会丢。原因和彻底还原方式见[组件完整性校验](#组件完整性校验)。

### 命令行

```bat
WxUnderlineSwitch.exe --cli status
WxUnderlineSwitch.exe --cli apply
WxUnderlineSwitch.exe --cli revert
```

可附加 `--target <RadiumWMPF.bin 路径>` 跳过自动定位。

---

## 界面上的状态含义

日志里会出现类似这样的一行：

```
0xF07B102   utf-16-le   patched   'null'
```

| 字段 | 含义 |
|---|---|
| `0xF07B102` | 命中位置在文件中的字节偏移 |
| `utf-16-le` | 该处的字符串编码形态（另一种可能是 `ascii`） |
| `patched` | 当前状态：`original` 未修改 / `patched` 已关闭 / `unknown` 认不出 |
| `'null'` | 该处的实际内容 |

---

## 技术细节

### 等长替换

| 项 | 值 |
|---|---|
| 目标文件 | `<微信安装目录>\<版本>\RadiumWMPF.bin` |
| 原文 | `true`（4 字符，UTF-16LE 占 8 字节） |
| 改为 | `null`（4 字符，UTF-16LE 占 8 字节） |
| 结果 | `WeixinJSBridge.support_mp_underline = null;` |

选 `null` 而不是 `false` 的原因有两个：

- **必须等长**。`false` 是 5 个字符，会破坏等长约束，导致文件后续所有偏移错位。
- **必须是假值**。`null` 在 JavaScript 里是 falsy，`ne` 直接为 `false`。

### 不写死偏移

工具每次运行都按特征串重新定位，而不是硬编码 `0xF07B102`：

```
按 8MB 分块流式扫描
  → 搜索 support_mp_underline（UTF-16LE 与 ASCII 双形态）
  → 解析其后的布尔字面量
  → 判定 original / patched
```

所以微信升级换版本、偏移变化后依然可用。

### 备份

改动前后的 SHA1 和各命中位置记录在：

```
%LOCALAPPDATA%\WxUnderlineSwitch\config.json
```

---

## 构建

需要带 tkinter 的 Python 3（python.org 官方安装包默认包含）。

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

脚本会依次完成：定位 Python → 建 venv → 装依赖 → `pyflakes` 静态检查 → GUI 冒烟测试 → 生成图标 → PyInstaller 打包 → 输出 `dist\WxUnderlineSwitch.exe` 及中文名副本。

### 开发期检查

```powershell
.venv\Scripts\python.exe -m pyflakes src\wx_underline_switch.py
.venv\Scripts\python.exe scripts\gui_smoketest.py
```

`gui_smoketest.py` 用 `unittest.mock` 把 `tkinter` 全部打桩，**不弹窗口**跑通 `run_gui()` 完整构造路径，适合无图形环境或 CI 使用。

---

## 项目结构

```
wx-underline-switch/
├── build.ps1                    一键构建（纯 ASCII，兼容 PowerShell 5.1）
├── requirements-build.txt       构建依赖
├── assets/app.ico               图标（由 scripts/make_icon.py 生成）
├── src/
│   └── wx_underline_switch.py   主程序：GUI + CLI
└── scripts/
    ├── make_icon.py             纯标准库生成 ICO
    ├── gui_smoketest.py         GUI 冒烟测试（打桩 tkinter，不开窗口）
    ├── zip_crc_test.py          ZIP CRC 修复逻辑的离线自检
    └── recon/                   定位 patch 点的侦察脚本，见该目录 README
```

---

## 微信更新后怎么办

微信大版本更新会重装 `RadiumWMPF.bin`，patch 被覆盖回 `true`。重新运行工具点一次「关闭划线」即可——这正是做成工具而不是一次性脚本的原因。

注意微信 4.x 是**静默自动升级**：不弹确认框，升级时把整个版本目录（如 `4.1.15.55`）换成新的（`4.1.15.56`），旧目录直接删掉。所以你可能完全没察觉，直到发现划线又回来了。

想确认当前版本，可以直接在启动器里搜版本串（微信的 PE 没有版本资源，`GetFileVersionInfo` 查不出来）：

```bat
findstr /C:"4.1.15" "C:\Program Files\Tencent\Weixin\Weixin.exe"
```

或者看 `C:\Program Files\Tencent\Weixin\` 下有几个版本目录、哪个是新建的。

如果新版本改了注入写法导致状态显示 `unknown`，可以用 `scripts/recon/` 里的脚本重新定位特征串。

---

## 组件完整性校验

`RadiumWMPF.bin` 不是普通文件，它是个 **ZIP 包**（46 个条目，全部 stored 不压缩），第一个条目是 `file_component.xml`：

```xml
<file_component checkvalue="1E6757E8..." signature="MGUCMG4ZKPK+...">
  <filter ... version="25799" />
  <file md5="b256f0a03bf1bc8613af8e860b62717c" length="237465648">runtime\flue.dll</file>
  ...
```

划线开关那条 JS 就落在 `runtime/flue.dll` 这个条目里。改完之后有三层完整性信息会对不上：

| 层 | 内容 | 能不能修 |
|---|---|---|
| ① ZIP 条目 CRC32 | 本地文件头 + 中央目录各记一份 | **能**，重算写回即可，不涉及任何密钥 |
| ② 清单 md5 + ECDSA 签名 | `file_component.xml` 里 `flue.dll` 的 md5，整份清单有 ECDSA 签名 | **不能**，私钥在腾讯手里 |
| ③ **`flue.dll` 自身的 Authenticode 签名** | PE 内嵌证书表，覆盖 `.rdata` 等节 | **不能**，且**这才是实际触发弹窗的那一层** |

### 第 ③ 层是怎么确认的

`ComponentVerification.dll`（腾讯官方签名）的导入表里有 `WinVerifyTrust` —— Windows 的 Authenticode 校验入口，配套的还有一整套 CRYPT32 调用。它验的是 PE 签名，**根本不看 ZIP 的 CRC32**。

补丁偏移 `0xC000710` 落在 `flue.dll` 的 `.rdata` 节（Raw `0xBC9D200 ~ 0xDB66600`），正是签名摘要覆盖的范围。改一字节签名必失效。

实测对比：

```
已打补丁   flue.dll  →  HashMismatch
原始版本   flue.dll  →  Valid
同目录其余文件        →  Valid
```

### 工具能修到哪一层

工具会自动处理第 ① 层：打完补丁后重算 `runtime/flue.dll` 的 CRC32 并写回两处，让包至少是一个**结构合法的 ZIP**。还原时 CRC 精确回到原值，因此可以无损回退（离线自检：`scripts/zip_crc_test.py`）。

②③ 两层无解。所以**打完补丁后微信会周期性弹出**：

> 微信遇到问题，将影响网页正常浏览，可重启微信后恢复。（立即重启 / 暂不处理）

选「暂不处理」时网页会显示 `STATUS_BREAKPOINT`。重启微信即恢复，补丁仍然生效。这个提示无法通过改文件消除，只能接受。

### 还原后还要清理解压副本

微信会把组件从 `RadiumWMPF.bin` 解压到用户目录再加载：

```
%APPDATA%\Tencent\xwechat\xplugin\plugins\RadiumWMPF\<版本号>\extracted\runtime\flue.dll
```

`--cli revert` 只还原 `.bin`，**不会动这份已解压的文件**。如果它是在补丁期间解压出来的，内容仍是被改过的、签名仍是坏的，下次启动可能继续用它。

彻底还原的方法（需先完全退出微信）：删除对应的 `extracted` 目录让微信重新解压，或从已还原的 `.bin` 里把 `runtime/flue.dll` 条目原样复制回去。

### 为什么不走运行时注入

理论上可以只改进程内存、让文件保持原样，从而绕开全部三层。这需要常驻一个后台进程，逐个接管新开的 `WeChatAppEx.exe`，并先 `VirtualProtectEx` 再写内存。改动面和维护成本都比本工具大得多，因此未实现。

---

## 风险与回滚

- 改动只涉及 `RadiumWMPF.bin` 里 `runtime/flue.dll` 条目的 8 个字节 + 该条目的 CRC32 字段，不影响 `Weixin.exe` / `Weixin.dll`。
- 会周期性触发「微信遇到问题，将影响网页正常浏览」提示，原因是 `flue.dll` 的 Authenticode 签名被破坏。重启微信即恢复，补丁不丢。这一层改不掉。
- 若微信无法启动或内置浏览器异常，退出微信后点「还原」，或执行 `--cli revert`。
- 还原会写回原始 8 字节、重算回原始 CRC32，并校验 SHA1 是否与改动前一致。
- **还原不会清理 `%APPDATA%\Tencent\xwechat\xplugin\...\extracted\runtime\flue.dll`**，那份解压副本可能仍是被改过的。彻底还原见[还原后还要清理解压副本](#还原后还要清理解压副本)。
- 微信升级前建议先还原，避免升级流程对文件做完整性校验时出现意外（见[组件完整性校验](#组件完整性校验)）。

---

## 已知限制

- 仅支持 Windows 桌面版微信。
- **改文件必然触发 Authenticode 校验失败**，表现为周期性弹窗。无法通过改文件规避，只能重启微信。详见[组件完整性校验](#组件完整性校验)。
- `--cli revert` 只还原 `.bin`，不清理 `xplugin` 下的解压副本。
- 未实现 per-monitor 动态 DPI 切换（Tk 8.6 支持有限）。修改系统缩放比例后需重启程序。
- 关闭后划线评论面板一并失效，这是该功能的整体能力，不是单独去掉视觉。
- 只对 Windows 有效。iOS / Android 的 `ios(>=8.0.32)`、`android(...)` 是独立通路，把 `support_mp_underline` 改成假值关不掉那边的划线。

---

## License

MIT
