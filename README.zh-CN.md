# shadowbot-studio-mcp

[English](README.md) · [Apache-2.0](LICENSE)

让 AI Agent 通过 MCP 读取和编辑 Windows 上的影刀 Studio 项目。服务使用标准 **stdio MCP** 接入 Agent，再连接影刀已有的本地 HTTP MCP，并提供应用身份核对、源码备份、过期修改拦截和写后回读。

当前版本 **1.2.0**。这是独立的社区桥接项目，需要另行安装并登录影刀。仓库只包含桥接代码和合成测试；工具定义在连接影刀时动态读取，不包含影刀程序、反编译源码或提取的工具契约文件。

## 获取代码

```powershell
git clone https://github.com/DevilHill/shadowbot-studio-mcp.git
cd shadowbot-studio-mcp
```

仅使用 Python 标准库，无需 `pip install`，无需构建。

## 环境要求

| 组件 | 要求 |
| --- | --- |
| 操作系统 | Windows x64，桥接服务与影刀使用同一个 Windows 用户运行 |
| [Python](https://www.python.org/downloads/windows/) | 64 位 Python 3.11 或更新版本；CI 覆盖 3.11–3.13 |
| [影刀](https://www.yingdao.com/) | 另行安装并登录，打开目标应用及 AI 面板 |
| MCP 客户端 | 支持启动本地 stdio 服务，允许访问本机进程、回环网络和项目文件 |
| [Git](https://git-scm.com/downloads) | 克隆代码需要；已有代码副本可直接运行 |

检查 Python：

```powershell
py -3 -c "import sys, struct; print(sys.executable, sys.version); assert sys.version_info >= (3, 11) and struct.calcsize('P') == 8"
```

本项目基于 **影刀 6.3.34** 开发。动态读取工具定义并不意味着自动兼容所有版本；影刀升级后，内部响应格式或进程发现方式仍可能变化。

## 快速开始

### 接入 Codex

先在影刀中打开目标项目和 AI 面板，使内嵌的 OpenCode 进程启动。无需向它发送 AI 请求。

安装 Codex CLI 后，在本仓库目录执行以下命令。先查看已有配置；如果 `shadowbot_studio` 已存在，请选一个未使用的名称试用公开版：

```powershell
codex mcp list
$bridgePython = (py -3 -c "import sys; print(sys.executable)").Trim()
$bridgeScript = (Resolve-Path .\stdio_server.py).Path
codex mcp add shadowbot_studio --env PYTHONUTF8=1 -- $bridgePython $bridgeScript
codex mcp get shadowbot_studio
```

在桌面端刷新 MCP 连接或重启后，让 Agent 先调用 `bridge_status`、`app_get_info` 和 `app_list_flows`，确认当前应用。配置方式参见 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

### 给其他 Agent 使用

服务不依赖 Codex。支持本地 stdio MCP 的 Agent 都可以使用同一个 Python 启动命令。

参考 [examples/mcp.example.json](examples/mcp.example.json)，将示例中的 Python 路径和 `stdio_server.py` 路径换成自己的绝对路径。示例采用常见的 `mcpServers` 外层结构，具体配置位置与外层字段按客户端要求调整。

服务必须运行在装有影刀的 Windows 机器上。此仓库没有提供远程托管端点；给其他人分享代码后，对方需要在自己的电脑配置影刀和项目目录。

### 本地运行与只读诊断

```powershell
py -3 .\stdio_server.py
```

该命令等待标准输入中的逐行 JSON-RPC，没有网页或启动横幅，通常应由 MCP 客户端启动。直接检查连接可以运行：

```powershell
py -3 .\diagnose.py
```

成功时输出 `connected: true`、应用 ID 和服务版本。诊断只读，不复制、保存或运行流程。

影刀未启动时，MCP 仍可以启动，但只提供 `bridge_status`。打开影刀后调用 `bridge_status`，参数为 `{"reconnect": true}`。工具清单变化时，服务会发送 `notifications/tools/list_changed`；客户端若不处理该通知，需手动刷新工具清单或重启 MCP 连接。服务不后台轮询。

### 支持的编辑功能

工具来自固定允许列表与当前影刀会话工具的交集，最多 31 个影刀工具，加上 `bridge_status`。例如 `codeflow.read` 在 Agent 中显示为 `codeflow_read`。

- 读取应用、流程、诊断、元素、变量、资源和指令文档。
- 创建、复制、重命名、删除及保存流程。
- 编辑 CodeFlow 源码、VisualFlow 指令块、参数、全局变量和资源。

服务不开放运行流程的工具。编辑仍会改变以后运行时的行为，应先在可丢弃的测试项目中验证。

所有写入都需要当前应用的 `expected_app_id`。源码修改时，将最近一次 `codeflow_read` 返回的 `_bridge.source_sha256` 作为写入参数 `expected_source_sha256`，以拦截过期修改。源码写入前会备份源码、参数和可视化调用方绑定，写后回读验证；其他编辑工具不提供同等源码备份保证。结果不确定时先检查实际状态和备份，不自动重试。

两种源码写入后端均要求已有源码非空且最多 2,000 行，因为影刀没有提供跨源码分页的原子快照。空流程先在 Studio 中添加 `main(args)` 函数；较大源码可分页读取，但拒绝写入。请求和响应的大小上限为 8 MiB。

### 可选：源码文件后端

某些影刀版本的原生源码更新接口会返回 `update_failed` / `flow_definition`。可以为指定项目启用文件后端：原子替换已有 CodeFlow 的源码文件，重新加载已保存的应用，核验源码与接口后再保存。未登记的项目继续走影刀原生接口。

在 `bridge.py` 同级建立 `.state/code_backends.json`，结构参照 [示例](examples/code_backends.example.json)。将应用 ID 和绝对 `xbot_robot` 路径替换为自己的值；不要填业务输入输出数据目录。

文件后端要求 `package.json` 和 `.dev/<flow_id>.flow.json` 均明确指向已有 CodeFlow。缺少元数据的 CodeFlow、VisualFlow 生成的 Python、磁盘与编辑器内容不一致、符号链接、junction 和硬链接都会被拒绝。后端不新建或改写流程元数据。

替换和重新加载前，影刀必须明确报告 `has_unsaved_changes: false`。先决定如何处理已有修改，需要保存时明确保存，再重新读取源码。结果不确定时不自动回滚，继续保存或重新加载前必须核对磁盘与编辑器状态。原生接口的修改可能保留为待保存状态，`app_save` 是独立的写操作。

同一应用保持**一个写入者**，包括人工编辑。多个 Agent 可共用同一服务目录，但文件后端的跨进程锁只协调共用 `.state` 目录的源码写入，不协调所有可视化操作或 Studio 人工修改。

### 常见问题

| 情况 | 处理方式 |
| --- | --- |
| 只有 `bridge_status` | 打开应用及 AI 面板，重连并刷新工具清单 |
| 同时打开多个影刀实例 | 在客户端服务环境中设置 `SHADOWBOT_OPENCODE_PID`，指定目标内嵌 `opencode.exe` 的 PID |
| 自动发现不可用 | 设置 `SHADOWBOT_MCP_URL` 为实际观察到的本机 HTTP 端点，不猜测端口 |
| 重启影刀后连接失效 | 调用 `bridge_status(reconnect=true)`，编辑前重新读目标内容 |
| 原生代码更新失败 | 检查错误，在测试项目中评估可选文件后端 |
| 未保存状态阻止写入 | 在 Studio 处理待保存修改，然后重新读源码 |

发现过程只读访问已核对的影刀子进程参数，只提取 `OPENCODE_STUDIO_MCP_URL`，不读取 OpenCode 配置文件、不注入代码、不转发 OpenCode 凭据。显式端点也仅允许 `127.0.0.1` 或 `::1` 上的 HTTP，不使用代理或跟随重定向。

`.state/` 默认被 Git 忽略，其中的备份可能包含完整源码及项目凭据，不能发布或原样分享。提交问题前检查诊断输出。Agent 客户端会收到请求的工具结果，使用前应按项目数据要求选择客户端。

## 开发与测试

在仓库根目录执行：

```powershell
py -3 -m unittest discover -s tests -v
```

测试使用临时目录、模拟 Studio 和本机 HTTP 测试服务，不需要影刀，也不运行真实业务。GitHub Actions 在 Windows / Python 3.11、3.12、3.13 上执行同一套测试。CI 通过不代表新的影刀版本已经完成真实兼容验证。

源码职责说明见 [英文 README](README.md#build-test-and-development)。无需构建或远程部署，将代码保存在稳定、可写的目录中并注册 stdio 入口即可。

## 贡献与许可证

问题反馈请到 [GitHub Issues](https://github.com/DevilHill/shadowbot-studio-mcp/issues)，附版本、工具名和合成示例，不上传真实业务源码、凭据、`.state/`、聊天数据库或完整诊断转储。贡献要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。

本仓库桥接代码采用 [Apache License 2.0](LICENSE)，另见 [NOTICE](NOTICE)。该许可证不覆盖另行安装的影刀软件或用户业务流程。本项目与影刀官方无隶属或背书关系。
