# 侦察脚本

用于定位 `RadiumWMPF.bin` 中划线开关的 patch 点。这些脚本记录了「怎么找到那条注入 JS」的过程，微信改版后可以用同样的方法重新定位。

全部只依赖 Python 标准库，不需要 IDA / Ghidra / radare2。

## 环境变量

微信安装目录不是默认路径时（例如跑在 Sandboxie 沙盒里），先设置：

```bat
set WX_BASE=D:\Sandbox\<用户名>\<沙盒名>\drive\C\Program Files\Tencent\Weixin\4.1.15.55
```

未设置时使用脚本内的默认路径（`C:\Program Files\Tencent\Weixin\<版本>`），按自己机器上的实际版本号改。

## 脚本

| 脚本 | 用途 |
|---|---|
| `strings_recon.py` | 在若干目标文件里搜索 `support_mp_underline`、`get_appmsg_segment` 等特征串，输出命中偏移与上下文。ASCII 与 UTF-16LE 双形态 |
| `scan_all.py` | 全量侦察：对 `underline`（大小写不敏感）与中文「划线」做 ASCII / UTF-16LE / UTF-8 三形态统计。分块流式扫描，不会把 400MB 文件读进内存 |
| `ctx.py` | 打印指定偏移附近的字符串上下文，可按 ASCII / UTF-16LE / UTF-8 三种视图解码 |
| `hexdump.py` | 对命中点做十六进制 dump，用于确认精确字节边界，是决定「同长度替换」的依据 |

## 当时的结论

`Weixin.dll`（194 MB）里 `underline` 的 25 处 ASCII 命中**全部是噪声**：Qt 字体序列化（`QFont::fromString`）、Qt 无障碍（`QTextCharFormat::UnderlineStyle`）、PDFium 注释类型（`Highlight.Underline.Squiggly`）、笔记编辑器菜单、图标字体 glyph 名。

唯一与文章划线相关的是 `RadiumWMPF.bin` 里的一处 UTF-16LE 字符串：

```
WeixinJSBridge.support_mp_underline = true;
```

`ctx.py` 与 `hexdump.py` 里的偏移和 JOBS 列表是当时针对 4.1.15.55 记录的证据，换版本后需要重新跑 `strings_recon.py` 定位。

## 典型流程

```bat
python strings_recon.py        :: 确认特征串在哪个文件、什么编码
python scan_all.py             :: 看清全貌，排除同名噪声
python hexdump.py              :: 确认精确字节，判断能否等长替换
```
