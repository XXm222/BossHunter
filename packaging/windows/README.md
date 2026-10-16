# Windows x64 一键构建和安装包

目标：Windows 10/11、Intel/AMD 64 位。请把整个项目复制到 Windows 虚拟机的本地磁盘目录（例如 `C:\BossHunter`），不要只复制本文件夹。源码路径支持空格，建议避免虚拟机共享盘和过长路径。

解压 GitHub 源码 ZIP 时，不要保留两层带提交编号的长文件夹。请让 `C:\BossHunter\pyproject.toml` 和 `C:\BossHunter\packaging\windows\build.cmd` 直接存在。Node 的 npm 目录层级较深，Windows PowerShell 5.1 解压时可能超过路径限制；脚本会先检查 ZIP 中的实际路径长度，再解压。

## 构建电脑首次准备

1. 安装 **Python 3.12 x64**：[官方 3.12.10 x64 安装包](https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe)。文件名中的 `amd64` 就是 Intel/AMD 使用的 x64；可以与已有 Python 3.10 并存。保留 pip、Python Launcher、Tcl/Tk 组件。脚本检查 Python 版本和位数；其他版本不会直接用于构建。
2. 安装 **Inno Setup 6.3 或更新的 6.x**：<https://jrsoftware.org/isdl.php>。
3. 安装或更新 **Microsoft Visual C++ v14 x64 运行库**：[微软官方下载](https://aka.ms/vc14/vc_redist.x64.exe)。Python 本身可启动，不代表 PDF/OCR 所需的 C++ DLL 齐全。Windows 11 ARM64 上运行 x64 Python 也使用此安装包。[微软说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
4. 构建和测试需要联网；真机流程需要安装 **Google Chrome**。

构建电脑需要 Python、Inno Setup 和上述运行库。使用安装包的人不需要 Python、Conda、Node.js、npm 或 Inno Setup；仍需安装 Chrome。发布目录会携带经过架构校验的独立 x64 C++ 运行库，最终仍须通过下方的干净环境验收。

### UTM / Windows 11 ARM64 构建

Windows 11 ARM64 可作为构建主机，利用系统的 x64 应用兼容运行。**仍须安装 Python 3.12 的 Windows installer (64-bit/x64)，不能安装 ARM64 版 Python。** 最终目标仍为 Intel/AMD Windows x64，并非原生 ARM64 安装包。

脚本读取 Windows 系统架构与版本，允许 Windows 11 ARM64，拒绝 Windows 10 ARM64；随后检查 Python 的 PE 架构和包 ABI，确保确实是 x64。PyInstaller 在单独的已验证 x64 Python 进程中选择 x64 bootloader，构建后遍历 exe、dll、pyd 检查均为 x64，才执行离线自检并允许编译安装包。

微软的 x64 运行库安装包同时包含 ARM64 和 x64 文件，不能仅凭安装包名称认定宿主机中的 DLL 可用于 x64 发布。构建脚本从微软官方下载固定版本的独立 x64 CRT 组件，按 `vc-runtime.json` 校验 SHA-256，只提取 release x64 DLL，并逐个检查 PE 架构。spec 在依赖分析前显式加入这些文件，分析后再次固定所有同名 CRT 副本（包括被钩子作为 data 收集的副本），避免 ARM64 宿主机 DLL 覆盖发布文件。该步骤仅写项目构建缓存，不修改 Windows 系统目录。[微软运行库架构说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)

若之前用 ARM64 Python 创建过 `.build\windows\venv`，请先删除这个构建虚拟环境目录再重新构建，保留其他源码和用户数据。最终仍需在目标 AMD/Intel 电脑验收，Windows 11 ARM64 上的构建及自检不能代替目标电脑测试。

## 第一步：双击 `build.cmd`

脚本会：

- 在项目 `.build\windows\venv` 中创建隔离构建环境，先安装 PyInstaller 和时区数据。
- 下载并校验固定版本的微软 x64 CRT 组件，在 `.build\windows\vc-runtime` 中准备发布运行库；已下载且 SHA-256 正确的包会复用。
- 从 Node.js 官方下载 Node 22 Windows x64，并与官方 SHA-256 清单比对。
- 使用下载包中的 npm 执行 `npm ci` 和前端生产构建。
- 确认前端 `dist\index.html` 生成后，再安装项目及招聘、OCR、PDF 可选依赖。源码压缩包不含 dist，不能先安装项目，否则 Hatchling 会报 `Forced include not found`。
- 打包前在构建虚拟环境中分别验证 PyMuPDF 的 PDF 创建、读取和渲染，以及 RapidOCR 的模型加载和推理；每项限时 120 秒。导入失败或原生崩溃会立即停止并保留错误，避免打包到最后才发现构建环境缺 DLL。
- 用 `BossHunter.spec` 构建目录形式的程序，附带 Python、Node.js、前端、浏览器代理、Patchright 驱动、OCR 资源和时区数据。
- 使用项目现有 `src\bosshunter\web\frontend\public\favicon.svg` 的橙色 “BH” 设计生成 `assets\BossHunter.ico`，并应用到程序、安装程序和快捷方式图标。
- 对生成的程序执行离线自检：模块导入、资源、Node 版本、Patchright 本地驱动启动和 OCR 模型加载。自检不启动 BOSS 浏览器，也不执行平台请求或发消息。

成功产物：`dist\windows\BossHunter\BossHunter.exe`。**便携测试要复制整个 BossHunter 文件夹，不能只复制 exe。**

构建日志：`.build\windows\build.log`；自检结果：`.build\windows\smoke-result.json`；自检运行日志：`.build\windows\smoke-result.log`。自检失败会显示具体错误；超过 5 分钟会停止该自检进程树并报告超时。构建失败后窗口会保留错误，修复网络或依赖问题后可以再次双击。依赖安装需要一定时间和磁盘空间；构建采用当前可解析的依赖版本，实际版本记录在构建日志中。

若 Node 解压时报 `CHANGELOG.md` 路径不存在或提示 ZIP 路径过长，请将完整源码重新解压到 `C:\BossHunter` 后构建。该顶层文件名不一定是最早失败的位置，应检查 ZIP 内深层 npm 文件的完整路径。更换源码目录时不要直接搬用旧 `.build\windows\venv`：Python 虚拟环境含绝对路径，应在新位置重新创建；无需重新安装系统 Python、C++ 运行库或 Inno Setup。[Windows 路径限制](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation)、[Python 虚拟环境可移植性说明](https://docs.python.org/3/library/venv.html#how-venvs-work)

### PyMuPDF 报 `_extra` DLL 加载失败

先在项目根目录运行以下命令，无需重新安装所有依赖：

```bat
.build\windows\venv\Scripts\python.exe -c "import pymupdf; print(pymupdf.VersionBind)"
```

若这里也失败，说明未打包的环境本身有问题。PyMuPDF 官方记录过缺少 `MSVCP140.dll` 导致相同错误的情况，先安装或修复上方微软 x64 运行库，再运行该命令验证；不能仅凭此错误认定缺少的具体 DLL。[PyMuPDF 官方故障说明](https://pymupdf.readthedocs.io/en/latest/installation.html#problems-after-installation)

导入成功后，可以先运行完整的原生依赖检查，成功再双击 `build.cmd`：

```bat
.build\windows\venv\Scripts\python.exe packaging\windows\build_support.py --check-native
```

如果原环境检查通过而冻结产物自检仍失败，再检查打包收集与 DLL 搜索路径；保留对应日志，不要通过跳过自检或删除 PDF/OCR 依赖来完成安装包。

如果构建提示 `_internal\msvcp140.dll` 架构为 `0xaa64`，说明发布目录混入 ARM64 DLL；请更新整个 `packaging\windows` 文件夹（包含新增的 `vc-runtime.json`）再运行 `build.cmd`。已有 `.build\windows\venv` 可以保留，脚本会重新构建发布目录。不要删除架构检查，也不要把 ARM64 DLL 重命名后用于 Intel/AMD 安装包。

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

“请求统计”页可修改招聘端读取间隔、分页间隔、主动招呼间隔、每岗每轮人数和 HTTP 轮询间隔。点击“保存节流参数”后会列出改动并要求确认；取消会放弃本次编辑，不写配置。确认后保存，但正在运行的进程继续使用原节流参数，页面显示“待重启生效”。当前使用手动重启：退出 BossHunter 启动器后重新打开并启动系统；源码运行时需同时停止并重启 Web 和招聘监测进程。只重启 Web 而旧 worker 仍运行时，页面继续提示待重启。

Windows 登录状态读取使用同一个 Chrome 资料目录内的 `Default\Network\Cookies`（兼容旧位置 `Default\Cookies`）和 `Local State` 密钥文件，不能将独立 Chrome 的 Cookie 数据库与普通 Chrome 的密钥混用。使用者应在启动器打开的独立 Chrome 中登录；若本机读取或解密失败，页面会显示错误类型，排查时同时提供 Chrome 版本。安装包的离线自检不包含真实用户 Cookie 解密，需要在目标 Windows 电脑验证。

数据位置：`%LOCALAPPDATA%\BossHunter\`，其中 `logs` 保存日志，`chrome-profile` 保存默认独立浏览器资料。安装文件位置：`%LOCALAPPDATA%\Programs\BossHunter\`。升级会先清理安装目录中由 PyInstaller 管理的 `_internal` 运行库目录再复制新文件，避免旧依赖残留；升级和卸载不删除独立用户数据目录。

启动 Chrome 和 Node.js 时临时清理 PyInstaller DLL 搜索目录及外部子进程 PATH 中的打包目录，随后恢复启动器自身的 DLL 搜索设置，避免将 Python 运行库传给独立程序。Web 和 worker 仍使用原打包环境。[PyInstaller 官方说明](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application)

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
