# BabelDOC WebUI 增强版

本仓库基于官方 [funstory-ai/BabelDOC](https://github.com/funstory-ai/BabelDOC)
源代码进行扩展，只说明相对于原项目新增的功能。BabelDOC 翻译引擎的原有能力、命令行参数和
技术文档请查看官方仓库。

当前代码基于 BabelDOC `0.6.4`。

## 新增内容

### 本地网页界面

- 新增中文网页操作界面，可上传 PDF、配置翻译服务、启动和取消任务。
- 使用黄色主题，并针对桌面浏览器和窄屏窗口优化布局。
- 默认监听 `0.0.0.0:8787`，启动后自动打开本机浏览器，并允许局域网设备访问。
- 支持在页面中下载翻译结果，并在服务重启后继续查看已经完成的任务。

### 翻译服务设置

- 支持 OpenAI 兼容接口地址和 API Key。
- API Key 可写入本地磁盘，无需每次重新填写。
- Windows 下使用当前用户的 DPAPI 加密 API Key，网页接口不会回传密钥原文。
- 可从兼容接口读取模型列表，并通过下拉框选择模型。
- 并发 QPS 默认使用推荐值 `4`。
- 默认生成无水印文件。
- 新增推理强度选项：关闭、低、中、高。

### 进度与 Token 统计

- 翻译进度显示一位小数，减少长时间停留在同一个整数造成的误解。
- 每个处理阶段都会说明当前正在执行的工作。
- 进度暂时不变时显示“仍在处理”提示，用于区分模型等待和程序卡死。
- 显示总 Token、输入 Token、输出 Token、缓存命中 Token 和术语提取 Token。

### 术语表

- 自动术语提取默认关闭，因为它会额外调用模型并明显增加 Token 消耗。
- 开启前会在网页中显示 Token 成本提示。
- 首页显示术语总数和出现频率最高的前十项。
- 完整术语表直接在网页内搜索和查看，不需要下载 CSV 文件。

### Windows 启动器

- 新增 `start-web.bat`，双击即可创建环境、安装依赖并启动网页。
- 新增单文件 `BabelDOC-Web.exe`，负责后台启动服务、等待健康检查并打开浏览器。
- 自动检查并选择 64 位 Python `3.10` 至 `3.13`，优先使用 Python `3.12`。
- 自动搜索以下 Python 来源：
  - 项目中的 `.venv` 和 `.venv-web`
  - Windows `py` 启动器
  - 系统 `PATH`
  - Python 官方常见安装目录
  - Miniconda 和 Anaconda 常见安装目录
- 如果现有虚拟环境不兼容，会使用备用环境，不会覆盖原环境。
- 支持包含中文字符的项目路径。
- 可通过 `BABELDOC_PYTHON` 指定用于创建环境的 `python.exe` 完整路径。
- 启动日志保存在 `.babeldoc-web/launcher.log`。

只检查 Python 选择结果、不启动服务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-web.ps1 -CheckOnly
```

### Docker 与自动构建

- 新增 Dockerfile，以非管理员用户运行网页服务。
- 新增 `compose.yaml`，可一条命令启动、持久化和自动重启服务。
- 设置、任务记录和模型缓存支持通过 Docker 卷持久化。
- 新增 GitHub Actions 工作流，自动构建 `linux/amd64` 镜像。
- 镜像包含来源标签、健康检查、构建证明和软件物料清单。
- 公开镜像地址：`ghcr.io/ccawmiku/babeldoc-webui:latest`。

### 测试与边界检查

- 新增网页接口单元测试和边界测试。
- 覆盖设置保存、API Key 加密、模型读取、任务创建、参数校验、进度、Token、术语表、
  文件下载、取消任务和 Windows 启动器等场景。
- 当前共 `69` 项测试，网页后端覆盖率约为 `87.7%`。

## Windows 使用方法

### 使用完整压缩包

从 [BabelDOC WebUI v0.1.0](https://github.com/ccawmiku/BabelDOC-WebUI/releases/tag/webui-v0.1.0)
下载 `BabelDOC-WebUI-Windows.zip`，解压后双击根目录中的 `BabelDOC-Web.exe`。

第一次运行需要下载并安装 Python 依赖，因此耗时会比后续启动长。

### 从源码启动

```powershell
git clone https://github.com/ccawmiku/BabelDOC-WebUI.git
cd BabelDOC-WebUI
.\start-web.bat
```

本机访问地址：<http://127.0.0.1:8787>。同一局域网中的其他设备使用
`http://这台电脑的局域网IP:8787` 访问。

### 构建 EXE

先运行一次 `start-web.bat` 创建项目环境，然后执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows-exe.ps1
```

生成文件位于 `dist/windows/BabelDOC-Web.exe`。

## Docker 使用方法

### 使用 Docker Compose

仓库已经包含 `compose.yaml`。在仓库根目录执行：

```bash
docker compose up -d
```

查看运行状态和日志：

```bash
docker compose ps
docker compose logs -f
```

停止容器：

```bash
docker compose down
```

设置和任务记录保存在 `babeldoc-data` 卷中，模型缓存保存在 `babeldoc-cache` 卷中。
`docker compose down` 不会删除这两个卷。

### 使用 Docker 命令

```bash
docker run --rm -p 8787:8787 \
  -v babeldoc-data:/data \
  -v babeldoc-cache:/home/babeldoc/.cache/babeldoc \
  ghcr.io/ccawmiku/babeldoc-webui:latest
```

启动后，本机访问 <http://127.0.0.1:8787>；同一局域网中的其他设备使用
`http://Docker主机的局域网IP:8787` 访问。Docker 镜像中不包含 API Key，需要在网页中自行配置。

服务默认允许局域网访问，请只在可信网络中运行，并通过系统防火墙控制可访问设备。

## 本地数据

直接运行时，网页数据默认保存在 `.babeldoc-web/`：

- `settings.json`：网页设置和加密后的 API Key。
- `jobs/`：上传文件、任务状态和翻译结果。
- `launcher.log`：Windows EXE 启动日志。

这些目录以及虚拟环境、测试文件和构建产物均已加入 Git 忽略规则，不会上传到 GitHub。

## 与官方项目的关系

- 官方项目：[funstory-ai/BabelDOC](https://github.com/funstory-ai/BabelDOC)
- 本仓库保留官方仓库为 `upstream`，便于后续同步翻译引擎更新。
- 本仓库新增内容同样遵循项目原有的 [AGPL-3.0 许可证](LICENSE)。
- 原项目的使用说明、命令行参数和贡献指南以官方仓库为准。
