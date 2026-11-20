# ClashHelper

ClashHelper 将多个 Clash / Mihomo YAML 订阅合并成一个固定地址，提供 Windows 桌面订阅管理器和 GitHub Actions 定时更新。合并配置面向支持 Meta / Mihomo 的客户端，包括 OpenClash。

仓库代码不包含订阅地址或节点。每位使用者需要在自己的 GitHub 账号中准备订阅、Secret Gist 和所需凭据。

## 功能

- 管理多个订阅，保留同名节点并标记来源，可查看每个订阅的合并结果。
- 使用 `template.yaml` 定义域名规则和代理组，包括 OpenAI、Gemini、GitHub 和 AgentRouter 等服务的独立选择组。
- 用 GitHub Actions 抓取和合并，发布前使用 Mihomo 校验配置，失败时保留已发布的订阅。
- Windows 管理器通过 GitHub CLI 保存订阅，使用 Windows DPAPI 加密本机填写记录。

## 准备自己的仓库

1. 在 GitHub 上 Fork 此项目，并克隆你自己的 Fork。后续配置都在你自己的仓库完成。
2. 安装 Python 3.12 和 GitHub CLI，在终端运行 `gh auth login` 登录你的 GitHub 账号，并运行 `gh auth refresh -h github.com -s gist` 授予管理器读取 Gist 所需权限。
3. 在同一账号创建一个 **Secret Gist**，其中建立名为 `clash.yaml` 的文件。初始内容可以是 `proxies: []`。记录 Gist 地址中对应的 ID。
4. 为该账号创建具有 Gist 写入权限的 GitHub Token。在你的仓库中打开 **Settings → Secrets and variables → Actions → New repository secret**，添加两个 Secret：
   - `GIST_ID`：刚创建的 Secret Gist ID。
   - `GIST_TOKEN`：具有 Gist 写入权限的 Token。
5. 确认自己的 Fork 已启用 Actions。复制 `.clashhelper.example.json` 为 `.clashhelper.json`，填写 Fork 的 `账号/仓库名` 和刚才的 Gist ID。

Secret Gist 不会公开列出，但任何拿到其地址的人都可能读取节点配置。不要公开 Gist 地址、Token、订阅链接或 `.clashhelper.json`。

## 使用 Windows 桌面管理器

双击 `打开订阅管理.cmd`。首次启动会建立本地 Python 环境并安装依赖。

在管理页添加自己的 Clash / Mihomo YAML 订阅，点击 **保存并更新**。页面会把订阅链接写入你配置的仓库 Secret `SUBSCRIPTIONS`，并启动合并工作流。任务成功后，点击 **复制订阅地址**，在客户端导入。

保存会替换 `SUBSCRIPTIONS` 的完整列表。重新打开管理器时，只有原 Windows 用户能解密本机保存的填写记录。

也可以在仓库 Actions Secrets 中直接设置 `SUBSCRIPTIONS`，每行一个订阅链接，然后运行 **合并订阅** 工作流。高级筛选可用 `SITES_YAML`，格式见 `sites.example.yaml`。设置高级配置后，桌面管理器不会覆盖它。

工作流每 6 小时尝试更新一次，也支持手动运行。公开仓库不使用 GitHub Actions 缓存保存订阅节点；本次无法合并或内核校验失败时，保留已发布的 Gist 配置。

## 分流和配置

修改 `template.yaml` 可调整 DNS、规则和代理组。`OpenAI-Sub4` 是手动选择组，支持选择所有合并节点。`AgentRouter` 只匹配 `agentrouter.org`，默认直连，可手动切换节点。`profile.store-selected` 允许客户端保存手选结果。

OpenClash 的覆写设置、运行模式和已保存的选择会影响最终行为。节点是否能访问某个服务，需要在实际网络中验证。

## 本地运行和测试

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item sites.example.yaml sites.yaml
# 在 sites.yaml 填写自己的订阅地址
.\.venv\Scripts\python.exe helper.py template.yaml out.yaml
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`.clashhelper.json`、`sites.yaml`、本机加密记录、输出配置和本地缓存都被 `.gitignore` 排除，仍请在分享文件前检查内容。

## 来源与许可

本项目基于 [nonPointer/ClashHelper](https://github.com/nonPointer/ClashHelper)。上游仓库未标明项目许可证，本 Fork 保留上游来源和历史，不对上游代码另行授予许可。

桌面管理器源码及其配置读取脚本按 [MIT 许可](LICENSE-MANAGER)开放，适用文件范围见该许可证。模板中部分 AI 域名规则参考 VPSDance/ai-proxy-rules，其 MIT 许可保存在 `third_party/ai-proxy-rules.LICENSE`。
