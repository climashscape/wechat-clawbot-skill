---
name: comm-wechat
description: 通过微信官方 ClawBot 通道（iLink bot API）把消息/图片/文件发送到用户自己的微信。触发说法包括：「发微信」「推送到微信」「微信ClawBot」「发文件到我微信」「微信发我」「微信提醒我」「微信通知我」「任务跑完微信告诉我」「绑定微信」「配置微信ClawBot」「看/读我在微信发的消息」「wechat」。只支持发给绑定的用户本人，不支持好友/群聊。技能负责引导新用户完成全平台配置（扫码绑定、常驻保活服务、推送激活）、辅助日常收发与排障，并提供微信→agent 的反向通道。
---

# comm-wechat — 把消息/文件发到你自己的微信

> **一句话**：微信官方 ClawBot 通道（腾讯维护，无封号风险）让 agent 把文本、图片、文件推送到你微信里的「微信ClawBot」联系人；你在微信里发的消息也会落盘给 agent，双向可用。

**30 秒了解**

| | |
| --- | --- |
| ✅ 能做 | 发文本/图片/文件给自己；微信里发 `/status` 查通道；保活提醒可个性化；外部系统经本地 HTTP 桥推送；失败自动排队补发 |
| ❌ 不能做 | 发给好友/群聊；主动发语音/视频；绕过 24h 推送窗口（微信服务端硬限制） |
| 前提 | 微信 ≥ 8.0.70 且「设置 → 插件」里有 ClawBot（灰度中）；Python ≥ 3.10 |
| 成本 | 首次配置约 5 分钟，agent 全程引导；之后零维护 |

## agent 行为准则

### 必须先问用户的时刻（用 AskUserQuestion）

1. **扫码绑定 / 重新绑定**——需要用户拿起手机操作，先说明要做什么再开始。
2. **删除凭据、停用或卸载服务**——不可逆操作，先确认再动手。
3. **发送内容拿不准**——比如多个候选文件、内容有歧义、可能打扰对方（就是用户本人）时，给出选项让用户挑。
4. **开启 HTTP 桥**——会在本机新增一个监听端口，说明影响并征得同意。
5. **排查无果，或现场情况与本文档不符**——如实报告 + 给选项，不要猜着修。

### 可以自行推断、不必问的时刻

- 文件显示名、多条内容合并的措辞、重试细节等实现层小事。
- `status` 正常且窗口充足时，直接发送。
- 发送返回 `QUEUED` 时不必中断任务：内容已入队，用户下一条微信消息后会自动补发，在总结里提一句即可。

### 动手前的思考清单

1. 通道状态如何？（`status`：session / 窗口剩余 / 队列）
2. 这条消息值得占用约 1/10 的每日主动推送配额吗？能合并吗？
3. 如果发送失败，走的是哪条路径？（`QUEUED` → 用户消息触发补发）用户能感知吗？
4. 遇到本文档没写的情况：先看 daemon 日志（Linux：`journalctl --user -u wechat-clawbot -o cat`；macOS/Windows：状态目录下 `daemon.log`），基于证据决定下一步，再决定要不要问用户。

**平台约定**：本文所有 `python3` 在 Windows 上一律读作 `py -3`；`~/.local/state/wechat-clawbot/` 在 Windows 上即 `%USERPROFILE%\.local\state\wechat-clawbot`；可用环境变量 `WECHAT_CLAWBOT_STATE` 重定向整个状态目录（设置了它，下文所有路径以它为准）。

## 能力边界

- 只能发给**绑定的用户本人**，不能发好友、群聊，不能代发（代码已强制：`--to` 与回执都只认本人/已知联系人，daemon 忽略陌生人的消息）。
- **24h 推送窗口（微信服务端硬限制）**：只有用户给微信ClawBot发消息才会续期推送窗口，超期后服务端直接丢弃 bot 下发内容。这是平台规则（官方 issue Tencent/openclaw-weixin#202，2026-06 起未放开），任何客户端都绕不开。daemon 已内置三项自动化缓解：窗口剩余不足时自动推送一条保活提醒（回复任意内容即续期）；窗口失效期间的发送自动落盘排队，下一条消息一到即自动补发；主动推送接近平台配额（约 10 条/24h）时日志告警。
- 可发送：文本、图片、文件。协议不支持主动发语音/视频（官方 bot API 限制）。

## 首次配置向导（新用户全平台）

agent 按序执行，**每一步都要验证通过再进下一步**，验证证据向用户展示。凭据文件只检查存在性，不打印内容。

### 第 0 步：环境探测

0. **ClawBot 灰度预检（最先做，不满足就止损）**：请用户看手机微信「我 → 设置 → 插件」里有没有「ClawBot」入口，且微信版本 ≥ 8.0.70。没有 = 灰度未放量，绑定无法进行；如实告知用户等待放量，或改走其他通道（如企业微信机器人），不要白装。
1. `python3 --version`（macOS/Linux）或 `py -3 --version`（Windows），需 ≥ 3.10。
2. `python3 -c "import qrcode"` 检查二维码渲染库；缺失时 `python3 -m pip install --user qrcode`（纯 Python，仅用于终端显示登录码）。报 `externally-managed-environment`（Debian 12+/Ubuntu 23.04+/Fedora 等受管环境）就**跳过安装**，后续用链接方式扫码，不要加 `--break-system-packages`。
3. 判断平台：Linux / Darwin / Windows（`uname -s` 或 `platform.system()`）。
4. 代理环境：脚本默认**直连**微信 API（不继承 `http_proxy` 环境变量）；确需走代理时设 `WECHAT_CLAWBOT_PROXY=http://…` 再启动 daemon。

### 第 1 步：扫码绑定

后台运行（注意 `2>&1` 合并——关键标记在 stderr；路径用技能目录绝对路径）：

```bash
python3 -u <技能目录>/wechat_clawbot.py login > /tmp/wc-login.log 2>&1
```

从输出提取：
- ASCII 二维码 → 原样贴给用户扫；
- `[login] 二维码链接: <URL>` → 一并给出，浏览器打开即显示官方二维码（终端码扫不出时的兜底）。

请用户：微信扫码 → 手机上点「确认登录」。
验证：`~/.local/state/wechat-clawbot/credentials.json` 出现；日志出现 `[login] 绑定成功`。
失败分支：码过期脚本自动刷新最多 3 次；总时限 8 分钟，超时进程自行退出，重跑即可。用户扫完服务端仍显示等待（无「已扫码」日志）→ 杀掉该进程重新拉码；反复如此 → 换链接方式扫码。

### 第 2 步：常驻保活服务（按平台三选一）

daemon 作用：长轮询保活 + 捕获推送锚点（context_token）+ 窗口看护 + 入站闭环。

**Linux（systemd user）**——`mkdir -p ~/.config/systemd/user` 后写入 `~/.config/systemd/user/wechat-clawbot.service`（`ExecStart` 里的 python3 建议换成第 0 步探测到的绝对路径；需代理时去掉 `Environment` 行的注释）：

```ini
[Unit]
Description=WeChat ClawBot keepalive daemon (iLink bot API)
After=network.target

[Service]
ExecStart=/usr/bin/env python3 -u <技能目录绝对路径>/wechat_clawbot.py daemon
# Environment=WECHAT_CLAWBOT_PROXY=http://127.0.0.1:7890
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

然后：`systemctl --user daemon-reload && systemctl --user enable --now wechat-clawbot.service`。
验证：`systemctl --user is-active wechat-clawbot` 为 active；`journalctl --user -u wechat-clawbot -o cat` 出现 `[daemon] running`。
开机自启（免登录）：`pkexec loginctl enable-linger $USER`（无 pkexec 用 `sudo`）。
无 systemd 的环境用 cron 兜底：`@reboot python3 -u <路径>/wechat_clawbot.py daemon >> ~/.local/state/wechat-clawbot/daemon.log 2>&1`（无崩溃自动拉起，需自行监控）。

**macOS（launchd）**——写入 `~/Library/LaunchAgents/org.comm-wechat.clawbot.plist`。注意：launchd 的 PATH 很短，`ProgramArguments` 里 **python3 必须写第 0 步探测到的绝对路径**（`command -v python3`），不能用 `/usr/bin/env`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.comm-wechat.clawbot</string>
  <key>ProgramArguments</key><array>
    <string><python3 绝对路径></string><string>-u</string>
    <string><技能目录绝对路径>/wechat_clawbot.py</string><string>daemon</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string><状态目录>/daemon.log</string>
  <key>StandardErrorPath</key><string><状态目录>/daemon.log</string>
</dict></plist>
```

需要代理时加 `EnvironmentVariables` 键（dict，含 `WECHAT_CLAWBOT_PROXY`）。
然后：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.comm-wechat.clawbot.plist`。
验证：`launchctl print gui/$(id -u)/org.comm-wechat.clawbot` 状态为 running；日志看状态目录 `daemon.log`。

**Windows（任务计划程序）**——用 `python.exe` + `--logfile`（不要用 `pythonw`：它没有 stdout/stderr，日志会全部丢失）：

```bat
schtasks /Create /TN "WeChatClawBot" /SC ONLOGON ^
  /TR "\"<python.exe 绝对路径>\" -u \"<技能目录>\wechat_clawbot.py\" daemon --logfile \"%USERPROFILE%\.local\state\wechat-clawbot\daemon.log\"" /F
schtasks /Run /TN "WeChatClawBot"
```

若报 `Access is denied`（非管理员常见），改用启动文件夹：`Win+R` → `shell:startup` → 放一个含同款命令的 `.bat`（`start "" /min <命令>`）。需代理时 `setx WECHAT_CLAWBOT_PROXY http://…` 后重新登录生效。
验证：`schtasks /Query /TN WeChatClawBot`；日志看 `%USERPROFILE%\.local\state\wechat-clawbot\daemon.log`。建议先在前台 `py -3 -u <路径>\wechat_clawbot.py daemon` 跑通再挂后台。

### 第 3 步：激活推送锚点

请用户**在微信里给「微信ClawBot」发任意一条消息**（如「1」）。
验证：`context_tokens.json` 出现 userId 键；服务日志出现 `captured context_token`；同时会看到回执「✅ 已收到」（入站闭环生效）。
2 分钟内没出现 → 先确认第 2 步服务真的在跑（三平台各自的验证命令），再让用户重发一条。
没有这一步，发送会返回 `ret=-2` 且微信静默丢弃。

### 第 4 步：端到端验收

```bash
# Windows 把 python3 换成 py -3；先造两个小测试文件
echo test > /tmp/wc-test.txt          # Windows: echo test > %TEMP%\wc-test.txt
python3 -c "from PIL import Image" 2>/dev/null || true
python3 wechat_clawbot.py send --text "comm-wechat 配置完成"
python3 wechat_clawbot.py send --file /tmp/wc-test.txt
python3 wechat_clawbot.py send --image <任意一张本机 jpg/png，没有就跳过此条>
```

每条返回 `OK -> <userId>` 且用户在微信里**亲眼看到**才算通过。请用户核对 3 条都收到。

## 日常使用

### 场景 → 用法

| 场景 | 推荐做法 |
| --- | --- |
| 长任务完成（编译/下载/训练） | `send --text "<一句话结果>"` + `send --file <产物>`；产物大就只发路径说明 |
| 定时日报/监控告警 | 系统 cron / 计划任务调 CLI；告警类即时发，不凑批 |
| 外部系统触发（CI/HomeAssistant/脚本） | 开 `bridge.json`（见下）后 `curl -X POST http://127.0.0.1:8787/send`，无需本机 agent 在场 |
| 批量多条内容 | **合并为一条**再发（平台配额约 10 条主动推送/24h） |
| 用户在微信里查状态 | 用户发 `/status`；bot 自动回复通道状态（daemon 内置） |
| 反向通道（微信 → agent） | 用户消息自动落盘 `inbox/*.json`，agent 扫描该目录即可接指令 |

### 命令

- 发文本：`send --text "内容"`；发文件：`send --file <路径>`（`--as-name` 改显示名）；发图片：`send --image <路径>`。文件上限 20MB（超限报错并提示压缩）。
- 会话健康：`status`（session、窗口剩余、队列、联系人）
- 指定目标：`--to <userId>`（默认发给绑定的自己；只接受本人或已知联系人，其余报错）
- **退出码语义**：`0` = 已送达**或已入队待补发**（输出 `QUEUED -> …`，此时**严禁重试**——队列按内容去重后仍会补发一条）；`1` = 参数/文件/绑定等硬错误。daemon 自动补发不重复计费你的注意力。

### 入站闭环（daemon 内置，无需配置）

用户从微信发来的每条消息会依次：续期 24h 窗口 → 落盘 `inbox/` → 触发队列补发 → 得到回执（非绑定用户的消息只记录日志，不回执）。
内置指令：`/status`（回通道状态）、`/help`（回用法）；其他消息回「✅ 已收到，推送窗口已续期 24 小时」（60 秒防抖，省配额）。
`inbox/` 条目 schema：`{"uid": "<发送者>", "text": "<文本内容>", "ts": <unix秒>}`——**媒体消息只落 `[媒体消息]` 占位，内容不落盘**（反向通道目前只支持文本指令）。

### 保活个性化（可选）

默认到点只发续期提醒。写 `~/.local/state/wechat-clawbot/keepalive.json` 可让每个用户的保活消息变成有用内容：

```json
{
  "enabled": true, "lead_minutes": 240,
  "text": "静态文案…",
  "file": "~/today-brief.txt",
  "users": { "<userId>": { "text": "该用户专属文案", "lead_minutes": 120 } }
}
```

`file` 指向的文件内容会作为正文（超 1500 字符截断；适合配合 cron 定时生成天气/日报/日程——内容生产归 cron，daemon 只管送达）；发送时自动在末尾追加续期提示。配置文件形状写错会被忽略并在日志提示，不会弄垮 daemon。

### 本地 HTTP 桥（可选，默认关）

`bridge.json` 由 daemon **首次启动时自动生成**（先跑起来一次再改配置）。把 `enabled` 改为 `true` 并重启 daemon 后，外部系统可从本机推送（`GET` 探活同样需要 token）：

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('$HOME/.local/state/wechat-clawbot/bridge.json'))['token'])")
curl -s -X POST http://127.0.0.1:8787/send -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{"text": "来自 CI 的通知"}'
```

仅监听 127.0.0.1 + Bearer token。成功 `{"result":"ok","ret":0}`；已入队 `{"result":"queued",...}`；其余 400/401/404/500 带 `{"error":...}`。注意：持有 token 即可在本机以 `file` 参数发送任意可读文件，token 等同用户信任级，勿外泄。

### agent 使用守则

1. 发送前不确定通道状态时先跑 `status`：`EXPIRED` 先走重新绑定；窗口剩余 >1 小时直接发；不足 1 小时且内容不紧急时，先提醒用户回复续期（daemon 在剩余不足时也会自动推个性化提醒）。
2. 看到 `QUEUED`（退出码 0）不要重试、不要中断任务：内容已入队，用户下一条微信消息到达后 daemon 自动补发，在总结里提一句即可。
3. 批量发送请间隔 ≥2 秒，避免触发风控节奏；主动推送注意配额（见下）。
4. 文件 ≤20MB；AES 为纯 Python 实现，吞吐约 1MB/s，大文件慢属预期。

## 故障排查

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| 发送返回 `QUEUED`（CLI 已自动入队） | 24h 窗口失效，锚点过期 | 用户给微信ClawBot回任意一条消息，daemon 自动补发，无需手动重试 |
| 收到「推送窗口约 N 分钟后过期」提醒 | daemon 窗口看护触发（临近过期） | 在微信里回复任意内容即续期 24h |
| `status` 显示 EXPIRED，或日志 `errcode -14` | 登录态过期 | 删 `credentials.json` → 重新 `login` → 重启保活服务 |
| 终端二维码显示乱/扫不出 | 缺 qrcode 库或终端字体问题 | `pip install --user qrcode`；或用日志里的链接浏览器扫码 |
| 扫码后手机无确认页 | 扫到了失效旧码 | 重新拉码；确认 URL 以 `liteapp.weixin.qq.com` 开头 |
| 服务反复重启 | 凭据缺失（daemon 立即退出） | 看日志（Linux `journalctl --user -u wechat-clawbot`；macOS/Windows 状态目录 `daemon.log`）；先完成 login 再 enable |
| `login` 报网络错误 / 无法获取二维码 | 到 `ilinkai.weixin.qq.com` 的连通性问题 | 检查网络与防火墙；需代理设 `WECHAT_CLAWBOT_PROXY` 后重试 |
| 挂了全局代理后连不上/超时 | urllib 默认继承 `http_proxy`（当前版默认直连，老版本会中招） | 升级到当前版脚本；确需代理设 `WECHAT_CLAWBOT_PROXY` |
| 笔记本合盖休眠后窗口过期 | daemon 随系统休眠停摆 | 唤醒后服务自动恢复；休眠期错过的提醒/推送按队列机制补发或顺延；对时效敏感就设置不休眠 |
| 日志出现 `[quota]` 告警 | 本地 24h 成功推送计数达 8 条（告警阈值，保守于社区观察到的平台配额约 10 条/24h；平台真实口径未公开，对消息的应答类可能豁免） | 削减推送频率、合并消息；保活提醒与回执都计入本地计数 |

## 安全与隐私

- `credentials.json` 是完整会话凭据，泄露即等于微信 ClawBot 通道被接管：立即删文件重新扫码绑定（旧 token 同时作废）。
- 状态目录权限 0700、凭据 0600，创建时设置（已有目录权限过宽请手动 `chmod 700` 收紧）。
- 所有网络请求锁定 `*.weixin.qq.com` 且仅 https，拒绝重定向；无本地监听端口（HTTP 桥开启后仅监听 127.0.0.1 且需 Bearer token）。
- 展示/分享诊断信息时只给文件名与日志结论，不贴凭据内容。

## 卸载 / 回滚

1. 停服务：Linux `systemctl --user disable --now wechat-clawbot.service` 并删 unit 文件；macOS `launchctl bootout gui/$(id -u)/org.comm-wechat.clawbot` 并删 plist；Windows `schtasks /Delete /TN WeChatClawBot /F`。
2. 删状态：`rm -rf ~/.local/state/wechat-clawbot`。
3. 删技能目录。全部可逆：重新走配置向导即可恢复。

## 分享给新用户

把整个 `comm-wechat/` 目录原样拷走即可，脚本内无机器特定配置（状态目录按用户主目录自动展开）。新用户在 agent 协助下从「首次配置向导」第 0 步开始走。
