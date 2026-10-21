# 知间 · ICON SLOT（交接给 Codex）

这个文件说明**图标槽位**在哪里、替换什么、怎么验证。  
当前已经预留好位置和加载链路；后续只需要生成一张图并替换文件，不需要改 UI 代码。

---

## 1. 唯一需要替换的文件

```
assets/zhijian-logo.png
```

| 项目 | 要求 |
|---|---|
| 尺寸 | **1024 × 1024** PNG（正方形） |
| 背景 | **透明**（alpha 通道） |
| 圆角 | 不要自己画圆角/外框，macOS 会按系统规则裁切 |
| 内容 | 居中，四周留 ≥10% 安全边距；缩小到 32px 仍能辨认 |
| 体积 | 建议 < 500 KB；太大只影响仓库，不影响运行 |

替换后**同时生效**于：

- 仪表盘左上角品牌图标（`/icon` 路由读取同一文件）；
- `知间.app` 的应用图标（打包时由 `build_app.py` 生成 `.icns`）；
- 菜单栏图标的图片来源参考（见下方第 4 节）。

> 如果文件缺失：仪表盘显示虚线槽位 + 「知」字占位符，不算错误。

---

## 2. 代码里的标注位置

| 位置 | 文件 | 说明 |
|---|---|---|
| `<!-- ICON SLOT ... -->` | `dashboard2.html` 顶部 | 品牌图标槽位、尺寸与约束 |
| `.brand-icon` / `#brandIcon` | `dashboard2.html` | 46×46 显示区；`onerror` 回退到「知」 |
| `LOGO` 常量 + `/icon` | `wvcore2.py` | 服务 `assets/zhijian-logo.png` |
| `// ICON SLOT` 注释 | `menubar.swift` | 状态栏图标来源说明 |
| `--icon` | `build_app.py` | 打包时生成 `.icns`（默认读 `assets/zhijian-logo.png`） |

生成图标时**不要改动**：

- `id="brandIcon"`、`#brandFallback`；
- `/icon` 路由；
- `.brand-icon` 的 46×46 尺寸；
- `build_app.py` 的 bundle id `local.zhijian.app`。

---

## 3. 替换与打包

```bash
cd /path/to/wvbar
# 1) 用新图覆盖槽位
cp /path/to/new-icon-1024.png assets/zhijian-logo.png
# 2) 重新编译菜单栏（Swift）
bash build.sh
# 3) 重新打包并安装知间.app（覆盖 /Applications）
python3 build_app.py --destination "/Applications/知间.app"
# 4) 重启应用
open -a "知间"
```

验证：

```bash
# 仪表盘图标
open -a Safari http://127.0.0.1:8765
# 应用图标
ls -l "/Applications/知间.app/Contents/Resources/AppIcon.icns"
```

> 如果 `知间.app` 正在运行，先退出再安装；安装后重新打开。

---

## 4. 菜单栏图标（可选）

菜单栏状态图标有独立的查找路径，不读取项目目录：

```
~/Library/Application Support/wvbar/icon.png
~/Library/Application Support/wvbar/icon.tiff
```

如果希望菜单栏也使用新图标：

```bash
mkdir -p "$HOME/Library/Application Support/wvbar"
# 建议 44×44 或 88×88（@2x），模板图标用纯黑+透明
cp assets/zhijian-logo.png "$HOME/Library/Application Support/wvbar/icon.png"
```

菜单栏空间小，建议另做**单色模板版**（纯黑 + 透明），不要直接用 1024 彩色图。

---

## 5. 建议给 Codex 的生成提示词

> 为 macOS 菜单栏应用「知间」设计应用图标：1:1 正方形，1024×1024，透明背景；  
> 主题是“聊天关系与温度变化”，可用两个交叠的对话气泡 + 一条细微的上升/下降趋势线；  
> 极简、深色背景友好、缩小到 32px 仍清晰；不要文字、不要圆角外框；主体居中，四周留 10% 安全边距。

生成后只需替换 `assets/zhijian-logo.png`，按第 3 节打包即可。
