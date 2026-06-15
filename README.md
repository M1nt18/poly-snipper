# Poly Snipper 截图工具

一个自用的 Windows 截图工具，支持全局快捷键、托盘运行、截图后标注、复制、保存和在线更新。

## 使用

推荐安装 `PolySnipperSetup.exe`。安装时勾选开机启动后，程序会随 Windows 自动启动，并常驻托盘。

源码方式运行：

```powershell
python poly_snipper.py
```

## 操作

- `Alt + A`：开始区域截图
- 鼠标拖动：选择截图区域
- `Esc`：取消截图
- 截图后：自动复制到剪贴板、保存 PNG，并打开置顶编辑窗口
- 编辑工具：移动、画笔、矩形、圆圈、箭头、文字、颜色、线宽、撤销
- `✥`：选择并拖动已经画好的图形或文字
- `复制`：复制编辑后的图片
- `另存为`：保存编辑后的图片
- `检查更新`：从 GitHub Releases 下载并运行最新版安装器

截图默认保存到：

```text
%USERPROFILE%\Pictures\PolySnips
```

## 说明

程序使用 Python、Tkinter、Pillow 和 Win32 API，不需要 .NET SDK。

## 打包 EXE 和安装器

```powershell
.\build_installer.ps1
```

勾选开机启动时，安装器会写入当前用户的启动项：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run\PolySnipper
```
