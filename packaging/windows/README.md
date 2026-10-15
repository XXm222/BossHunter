# Windows x64 一键构建和安装包

目标：Windows 10/11、Intel/AMD 64 位。请把整个项目复制到 Windows 虚拟机的本地磁盘目录（例如 `C:\BossHunter`），不要只复制本文件夹。源码路径支持空格，建议避免虚拟机共享盘和过长路径。

## 构建电脑首次准备

1. 安装 **Python 3.12 x64**：<https://www.python.org/downloads/windows/>。保留 pip、Python Launcher、Tcl/Tk 组件。脚本检查 Python 版本和位数；其他版本不会直接用于构建。
2. 安装 **Inno Setup 6.3 或更新的 6.x**：<https://jrsoftware.org/isdl.php>。
3. 构建和测试需要联网；真机流程需要安装 **Google Chrome**。

只需在构建电脑安装 Python 和 Inno Setup。使用安装包的人不需要 Python、Conda、Node.js、npm 或 Inno Setup；仍需安装 Chrome。

### UTM / Windows 11 ARM64 构建

Windows 11 ARM64 可作为构建主机，利用系统的 x64 应用兼容运行。**仍须安装 Python 3.12 的 Windows installer (64-bit/x64)，不能安装 ARM64 版 Python。** 最终目标仍为 Intel/AMD Windows x64，并非原生 ARM64 安装包。

脚本读取 Windows 系统架构与版本，允许 Windows 11 ARM64，拒绝 Windows 10 ARM64；随后检查 Python 的 PE 架构和包 ABI，确保确实是 x64。PyInstaller 在单独的已验证 x64 Python 进程中选择 x64 bootloader，构建后遍历 exe、dll、pyd 检查均为 x64，才执行离线自检并允许编译安装包。

若之前用 ARM64 Python 创建过 `.build\windows\venv`，请先删除这个构建虚拟环境目录再重新构建，保留其他源码和用户数据。最终仍需在目标 AMD/Intel 电脑验收，Windows 11 ARM64 上的构建及自检不能代替目标电脑测试。

## 第一步：双击 `build.cmd`

脚本会：

- 在项目 `.build\windows\venv` 中创建隔离构建环境并安装 PyInstaller、项目依赖及招聘、OCR、PDF 可选依赖。
- 从 Node.js 官方下载 Node 22 Windows x64，并与官方 SHA-256 清单比对。
- 使用下载包中的 npm 执行 `npm ci` 和前端生产构建。
- 用 `BossHunter.spec` 构建目录形式的程序，附带 Python、Node.js、前端、浏览器代理、Patchright 驱动、OCR 资源和时区数据。
- 对生成的程序执行离线自检：模块导入、资源、Node 版本、Patchright 本地驱动启动和 OCR 模型加载。自检不启动 BOSS 浏览器，也不执行平台请求或发消息。

成功产物：`dist\windows\BossHunter\BossHunter.exe`。**便携测试要复制整个 BossHunter 文件夹，不能只复制 exe。**

构建日志：`.build\windows\build.log`；自检结果：`.build\windows\smoke-result.json`。构建失败后窗口会保留错误，修复网络或依赖问题后可以再次双击。依赖安装需要一定时间和磁盘空间；构建采用当前可解析的依赖版本，实际版本记录在构建日志中。

## 第二步：双击 `installer.cmd`

脚本自动查找 Inno Setup 6 的 `ISCC.exe` 并编译 `BossHunter.iss`。如果安装在自定义位置，可以把 ISCC 所在目录加入 PATH，或在 Inno Setup 中打开 `.iss` 按 F9 编译。

**双击 `.iss` 通常只打开编辑器，不会自动编译；要真正双击即打包，请使用 `installer.cmd`。**

成功产物：`dist\installers\BossHunter-Setup-<版本>-windows-x64.exe`。

安装包只收集 PyInstaller 的发布目录，不收集项目的 `config.yaml`、私有凭据、`data`、浏览器资料或简历。程序版本从源码读取，不需要手动改 `.iss`。

## 使用者操作

1. 安装 Chrome，再运行安装包，无需管理员权限。
2. 从开始菜单运行 BossHunter，点击“启动系统”。
3. 启动器打开独立 Chrome，并启动浏览器代理、Web 服务和 worker；服务就绪后打开招聘页面。
4. 在独立 Chrome 登录自己的 BOSS，在项目网页配置模型、选择岗位并确认绑定会话。首次保存模型配置后停止系统再启动，让独立 worker 读取最新配置。自动任务仍由项目内的开关控制，安装或打开启动器不会自行开启任务；已有数据中开启的任务按原项目逻辑恢复。
5. “停止系统”或关闭启动器会停止本次启动的服务和独立 Chrome，保留登录资料及数据库；普通 Chrome 不受影响。

数据位置：`%LOCALAPPDATA%\BossHunter\`，其中 `logs` 保存日志，`chrome-profile` 保存默认独立浏览器资料。安装文件位置：`%LOCALAPPDATA%\Programs\BossHunter\`。升级和卸载不删除用户数据。

默认使用本机端口 8686（Web）、3456（代理）、9222（Chrome）。端口冲突会停止启动并显示原因，不会杀掉其他软件的进程。第二个启动器不会重复启动服务。启动器异常退出时，Windows Job Object 会清理其管理的进程树。

## Windows 验收清单

- 先运行构建产物，再安装打包产物；确认“启动、打开页面、查看日志、停止、再次启动”均正常。
- 检查普通 Chrome 已打开时仍使用独立资料，停止后普通 Chrome 保持打开。
- 检查模型配置、BOSS 登录 Cookie 读取、岗位与会话绑定、WS/HTTP 监测、简历读取和评分。跨系统后的 Cookie 读取尤其需要实测，打包自检不能代替平台验收。
- 检查重复启动、端口冲突和缺少 Chrome 时显示明确原因，失败后没有遗留服务占用端口。
- 检查退出或异常结束启动器后，后台及其子进程退出；检查重新启动后仍能读取已有配置和数据库。
- 在未安装 Python/Node/Conda 的干净 Windows 用户环境测试；构建虚拟机本身已安装 Python，不能单凭它验证所有运行时都已打入包。
- 升级或卸载重装后，用户配置和数据仍保留。

当前脚本在 macOS 上开发，Windows 专用 PowerShell、Job Object、PyInstaller Windows 产物及 Inno Setup 编译须在虚拟机实际验证。Windows 11 ARM64 构建路径已加入架构检查，但尚未在 ARM64 虚拟机实测。安装包暂未进行数字签名。

参考：[PyInstaller spec](https://pyinstaller.org/en/stable/spec-files.html)、[Inno Setup 命令行编译](https://jrsoftware.org/ishelp/topic_compilercmdline.htm)。
