# 微信划线开关

关闭 / 还原微信 PC 版公众号文章里的「划线评论」绿色虚线标记。

划线评论是微信公众号的互动功能：读者选中某段文字发表评论后，该段文字下方会显示一条微信绿虚线，其他读者点它就能看到相关讨论。这个工具在客户端层面把这套能力关掉。

带图形界面，单文件 exe，绿色免安装。

**[下载最新版](https://github.com/xxhhlk/wx-underline-switch/releases/latest)** — `WxUnderlineSwitch.exe`，11.2 MB，无需安装运行时。

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

> 从 [Releases](https://github.com/xxhhlk/wx-underline-switch/releases/latest) 下载 `WxUnderlineSwitch.exe`，或自行构建（见「自己构建」一节，`build.ps1` 一条命令，会同时生成中文名副本 `微信划线开关.exe`），也可以直接 `python src\wx_underline_switch.py` 运行（需 Python 3.8+ 带 tkinter）。

1. **完全退出微信**（托盘图标右键退出，确认任务管理器里 `Weixin.exe` 和 `WeChatAppEx.exe` 都消失）
2. 双击 `WxUnderlineSwitch.exe`
3. 点「关闭划线」
4. 启动微信生效

工具会自动定位 `RadiumWMPF.bin`，覆盖普通安装目录和 Sandboxie 沙盒布局。识别不到时可用「浏览」手动指定。

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
    └── recon/                   定位 patch 点的侦察脚本，见该目录 README
```

---

## 微信更新后怎么办

微信大版本更新会重装 `RadiumWMPF.bin`，patch 被覆盖回 `true`。重新运行工具点一次「关闭划线」即可——这正是做成工具而不是一次性脚本的原因。

如果新版本改了注入写法导致状态显示 `unknown`，可以用 `scripts/recon/` 里的脚本重新定位特征串。

---

## 风险与回滚

- 改动只涉及 `RadiumWMPF.bin` 的 8 个字节，不影响 `Weixin.exe` / `Weixin.dll`。
- 若微信无法启动或内置浏览器异常，退出微信后点「还原」，或执行 `--cli revert`。
- 还原会写回原始 8 字节并校验 SHA1 是否与改动前一致。
- 微信升级前建议先还原，避免升级流程对文件做完整性校验时出现意外。

---

## 已知限制

- 仅支持 Windows 桌面版微信。
- 未实现 per-monitor 动态 DPI 切换（Tk 8.6 支持有限）。修改系统缩放比例后需重启程序。
- 关闭后划线评论面板一并失效，这是该功能的整体能力，不是单独去掉视觉。

---

## License

MIT
