# 影刀 MCP 接入指南

## 接入前准备

1. 在装有影刀的 **Windows 电脑**上安装 64 位 Python 3.11 或更新版本。
2. 打开影刀中的目标项目和 **AI 面板**，保持影刀运行。
3. 下载本仓库，或在 PowerShell 中执行：

```powershell
git clone https://github.com/DevilHill/shadowbot-studio-mcp.git C:\mcp\shadowbot-studio-mcp
```

查看本机 Python 的实际路径：

```powershell
py -3 -c "import sys; print(sys.executable)"
```

下文统一使用这两个示例路径，配置时替换为自己的实际路径：

| 项目 | 示例路径 |
| --- | --- |
| Python 程序 | `C:\Python313\python.exe` |
| MCP 入口文件 | `C:\mcp\shadowbot-studio-mcp\stdio_server.py` |

客户端选择 **本地 / stdio** 连接方式，由客户端启动 Python。无需另开终端运行服务，也无需安装额外 Python 依赖。Python 和影刀应在同一台 Windows 电脑、同一个用户下运行。

## 通用 JSON 配置

支持 `mcpServers` 配置的客户端，可以使用以下内容。已有其他 MCP 时，只合并 `shadowbot_studio` 条目，保留原有服务。

```json
{
  "mcpServers": {
    "shadowbot_studio": {
      "command": "C:\\Python313\\python.exe",
      "args": [
        "C:\\mcp\\shadowbot-studio-mcp\\stdio_server.py"
      ],
      "env": {
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

如果客户端使用表单添加服务，填写：

| 配置项 | 填写内容 |
| --- | --- |
| 名称 | `shadowbot_studio` |
| 类型 / 传输方式 | `stdio` 或“本地命令” |
| 命令 | Python 程序的绝对路径 |
| 参数 | `stdio_server.py` 的绝对路径，作为一个独立参数 |
| 环境变量 | `PYTHONUTF8=1` |

## Codex

### 使用 CLI 添加

在 PowerShell 中执行：

```powershell
codex mcp add shadowbot_studio --env PYTHONUTF8=1 -- "C:\Python313\python.exe" "C:\mcp\shadowbot-studio-mcp\stdio_server.py"
```

完成后刷新 Codex 桌面端的 MCP 连接，或重启 Codex。若已有同名服务，编辑已有配置，或换一个未使用的名称。

### 直接编辑配置

也可以编辑默认配置文件 `%USERPROFILE%\.codex\config.toml`，加入以下内容。两种添加方式任选一种。

```toml
[mcp_servers.shadowbot_studio]
command = 'C:\Python313\python.exe'
args = ['C:\mcp\shadowbot-studio-mcp\stdio_server.py']

[mcp_servers.shadowbot_studio.env]
PYTHONUTF8 = '1'
```

保存后刷新 MCP 连接或重启 Codex，并在本机执行的会话中使用。

[Codex 官方 MCP 配置文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

## WorkBuddy

1. 打开左侧 **专家·技能·连接器**，切换到 **连接器** 页签，点击 **自定义连接器**。部分版本的入口为 **插件 → MCP 服务器 → 配置 MCP**。
2. 添加本地 `stdio` 服务，填入上方表格中的命令、参数和环境变量；如果界面支持 JSON 配置，使用上方的通用 JSON。
3. 保存并启用 `shadowbot_studio`，在当前任务中使用该连接器。

也可以将通用 JSON 合并到以下任一配置文件：

- 用户级：`%USERPROFILE%\.workbuddy\mcp.json`
- 项目级：当前项目下的 `.workbuddy\mcp.json`

保存后刷新连接器，必要时重启 WorkBuddy。

[WorkBuddy 官方连接器入口](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector) · [MCP 配置说明](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide)

## Claude Desktop

1. 打开 **Settings → Developer → Edit Config**。
2. 将上方的通用 JSON 合并到打开的配置文件中。Windows 默认位置为：

```text
%APPDATA%\Claude\claude_desktop_config.json
```

3. 保存文件，完全退出并重新启动 Claude Desktop。
4. 在对话的连接器列表中启用 `shadowbot_studio`。

这里使用桌面端的本地服务配置入口。

[Claude Desktop 本地 MCP 配置指南](https://modelcontextprotocol.io/docs/develop/connect-local-servers)

## Claude Code

在 Windows PowerShell 中执行：

```powershell
claude mcp add --env PYTHONUTF8=1 --transport stdio --scope user shadowbot_studio -- "C:\Python313\python.exe" "C:\mcp\shadowbot-studio-mcp\stdio_server.py"
```

重新打开 Claude Code 会话，在会话中输入 `/mcp`，启用或重连 `shadowbot_studio`。`--scope user` 表示此用户的不同项目都可以使用该服务。

[Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp)

## DeepSeek Harness

DeepSeek Harness 使用 Cordis YAML 配置，按以下步骤添加本地服务。

1. 新建 `C:\mcp\shadowbot.cordis.yml`，写入以下内容，并替换实际路径：

```yaml
- insert:
    - id: mcp-shadowbot-studio
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: shadowbot_studio
        transport: stdio
        command: 'C:\Python313\python.exe'
        args:
          - 'C:\mcp\shadowbot-studio-mcp\stdio_server.py'
        env:
          PYTHONUTF8: '1'
        cwd: 'C:\mcp\shadowbot-studio-mcp'
```

2. 已安装 `dsh` 命令后，在 Windows PowerShell 中使用该配置启动：

```powershell
dsh web --patch 'C:\mcp\shadowbot.cordis.yml'
```

3. 在启动的本地 Web 界面中创建任务，使用 `shadowbot_studio` 提供的工具。

[DeepSeek Harness 官方 MCP 客户端配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.zh.md) · [官方 YAML 加载示例](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/mcp-memory.zh.md)

## 豆包模型：通过 Cherry Studio 接入

以下步骤使用 **火山方舟的豆包模型 API + Windows 版 Cherry Studio**。豆包 App / 网页版的原生本地 MCP 添加方式尚未在本文确认。

1. 在火山方舟开通支持工具调用的豆包模型，获取 API Key 和模型 ID。
2. 打开 Cherry Studio 的 **设置 → 模型服务**，添加 OpenAI 兼容服务商，填写：

| 配置项 | 填写内容 |
| --- | --- |
| API 地址 | `https://ark.cn-beijing.volces.com/api/v3` |
| API Key | 自己的火山方舟 API Key |
| 模型 ID | 在方舟开通的豆包模型 ID |

3. 打开 **设置 → MCP → MCP 服务器 → 添加**，选择本地命令 / stdio，填写上方通用配置表中的命令、参数和环境变量，保存并启用。
4. 打开 **工作 → Agent 菜单 → 编辑 → MCP**，为当前 Agent 启用 `shadowbot_studio`，并选择刚配置的豆包模型。
5. 返回该 Agent 的对话中使用。

[火山方舟接入 Cherry Studio 文档](https://console.volcengine.com/ark/region:cn-beijing/docs/integrate-third-party-tools) · [Cherry Studio MCP 设置文档](https://docs.cherryai.com.cn/advanced-basic/extensions/mcp)

## 其他支持 MCP 的 Agent

在客户端的 MCP 设置中添加 **本地 stdio 服务**，填入前面的命令、参数和环境变量。支持 `mcpServers` JSON 导入时，可以直接使用通用配置。

如果界面只接受 HTTP / SSE URL，不能直接填入 `stdio_server.py` 路径；需要使用支持本地 stdio 的客户端。

## 接入后开始使用

在 Agent 中发送：

> 使用 shadowbot_studio，读取当前影刀应用名称、应用 ID 和流程列表。

需要编辑时，直接说明目标流程和修改要求，例如：

> 读取当前项目的「订单处理」流程，先说明需要修改的位置，再按我的要求修改。

如果只看到 `bridge_status`，先打开影刀目标项目和 AI 面板，再发送：

> 调用 bridge_status，传入 reconnect=true，然后刷新影刀工具列表。

客户端未刷新工具列表时，重连该 MCP 服务或重新打开会话。
