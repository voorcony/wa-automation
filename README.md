# WhatsApp AI Auto-Reply System

一个基于 **AdsPower 指纹浏览器** + **DeepSeek AI** + **RAG 知识库** 的全自动 WhatsApp 聊单系统。支持多账号、AI 智能回复、三层站群支付集成。

## 架构

```
┌─ Windows (AdsPower Desktop) ──────────────────┐
│                                                │
│  AdsPower 桌面版 (全指纹浏览器)                  │
│  Chrome 144 + SOCKS5 代理                      │
│  Local API :50325                              │
│  CDP WebSocket :动态端口                        │
│                                                │
└──────────────┬── SSH 隧道 ─────────────────────┘
               │
               ▼
┌─ Linux Server ──────────────────────────────────┐
│                                                  │
│  wa-worker.js    ← puppeteer-extra + stealth     │
│     (whatsapp-web.js)                            │
│                                                  │
│  AI Engine (FastAPI)                             │
│     → DeepSeek v4 推理                           │
│     → RAG 产品知识库 (飞书 321 商品)              │
│                                                  │
│  Orchestrator                                    │
│     → 账号生命周期管理                            │
│                                                  │
│  Dashboard (FastAPI + HTML/JS)                   │
│     → 状态监控 / QR码显示 / 账号管理              │
│                                                  │
│  Nginx 反向代理                                  │
│     → /dashboard/ → Dashboard                    │
│                                                  │
└──────────────────────────────────────────────────┘
```

## 功能

- ✅ **AdsPower 指纹浏览器** — 真实 Windows 环境指纹（WebGL、Canvas、Audio、Timezone）
- ✅ **WhatsApp 全自动** — 扫码登录、消息监听、AI 回复
- ✅ **DeepSeek v4 AI** — 智能理解客户消息，推荐商品
- ✅ **RAG 知识库** — 321 款商品，语义检索 + 多轮上下文
- ✅ **SOCKS5 代理** — 每个账号独立美国家庭 IP
- ✅ **Web Dashboard** — 实时状态、QR 扫码、账号管理
- ✅ **自动重连** — SSH 隧道 + worker 健康检查守护

## 目录结构

```
wa-automation/
├── wa-worker/              # WhatsApp 工作进程 (Node.js)
│   ├── worker.js           # 主入口 — whatsapp-web.js + stealth
│   ├── browser_launcher.js # AdsPower 浏览器启动器
│   ├── message_handler.js  # 消息解析与路由
│   ├── queue.js            # 消息队列与重试
│   ├── config.js           # 配置加载
│   ├── logger.js           # 日志工具
│   └── package.json
├── ai_engine/              # AI 引擎 (Python FastAPI)
│   ├── main.py             # API 服务 :8082
│   ├── deepseek_client.py  # DeepSeek API 调用
│   ├── product_store.py    # 飞书商品库缓存 (321 商品)
│   ├── rag.py              # 语义检索 + prompt 构建
│   └── prompts.py          # 系统提示词
├── orchestrator/           # 账号编排 (Python)
│   └── main.py             # 账号生命周期管理 :8080
├── dashboard/              # 监控面板 (Python + HTML)
│   ├── main.py             # 代理 API :8086
│   └── static/index.html   # 暗色主题 SPA
├── config.yaml             # 全局配置
├── wa-healthcheck.sh       # 健康检查 (cron 每2分钟)
└── deploy.sh               # 部署脚本
```

## 快速开始

### 前置条件

| 组件 | 要求 |
|------|------|
| Windows 机器 | AdsPower 桌面版已安装，API 已开放到局域网 |
| Linux 服务器 | Ubuntu 22.04+, Node.js 20+, Python 3.12+ |
| 飞书多维表格 | 商品库已建立 (字段: 名称、价格、图片URL、描述等) |
| DeepSeek API | 有效的 API Key |

### 1. 克隆 & 安装

```bash
git clone https://github.com/voorcony/wa-automation.git
cd wa-automation

# 安装 Node.js 依赖
cd wa-worker && npm install && cd ..

# 安装 Python 依赖
pip install -r ai_engine/requirements.txt
pip install -r orchestrator/requirements.txt
pip install -r dashboard/requirements.txt
```

### 2. 配置

编辑 `config.yaml`：

```yaml
app:
  log_level: INFO

deepseek:
  api_key: "your-deepseek-api-key"
  model: "deepseek-chat"

wa_worker:
  host: "0.0.0.0"
  port: 8083
  state_dir: "./data/wa-sessions"

feishu:
  app_id: "your-feishu-app-id"
  app_secret: "your-feishu-app-secret"
  product_table: "tbl_xxxxx"
```

### 3. 启动流程（Linux + Windows 混合架构）

#### Windows 端

```powershell
# 1. 确认 AdsPower 已运行，API 已开放
curl http://127.0.0.1:50325/status/

# 2. 启动浏览器 profile
curl "http://127.0.0.1:50325/api/v1/browser/start?user_id=k1c9cdsg"
# 返回: {"data":{"ws":{"puppeteer":"ws://127.0.0.1:PORT/..."}}}
```

#### Linux 端

```bash
# 1. SSH 隧道 — 将 Windows CDP 端口转发到本地
sshpass -p 'password' ssh -L PORT:127.0.0.1:PORT -N user@windows-ip &

# 2. 启动 AI 引擎
cd wa-automation && python3 -m ai_engine.main &

# 3. 启动 Orchestrator
python3 -m orchestrator.main &

# 4. 启动 Worker（连接 Windows 浏览器 CDP）
cd wa-worker && node worker.js \
  --user-id=k1c9cdsg \
  --ws-endpoint="ws://127.0.0.1:PORT/devtools/browser/UUID" \
  --ai-url=http://localhost:8082 \
  --config=../config.yaml \
  --port=8083 &

# 5. 启动 Dashboard
cd .. && python3 -m dashboard.main &

# 6. 访问面板
# http://your-server:8086/ 或通过 nginx /dashboard/
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | — |
| `ADSPOWER_API_URL` | AdsPower Local API 地址 | `http://localhost:50325` |
| `WORKER_PORT` | Worker HTTP 端口 | `8083` |
| `AI_ENGINE_PORT` | AI 引擎端口 | `8082` |
| `ORCHESTRATOR_PORT` | 编排器端口 | `8080` |
| `DASHBOARD_PORT` | 仪表盘端口 | `8086` |

## Dashboard

访问 `http://<your-server>/dashboard/` 查看：

- 📊 **系统状态** — AdsPower、Redis、AI Engine、Orchestrator
- 👤 **账号管理** — 增删账号，启动/停止
- 🔗 **WhatsApp 登录** — QR 码扫码
- 📋 **最近活动** — 实时日志

## 健康检查

每 2 分钟由 cron 自动执行：

```bash
*/2 * * * * /home/ubuntu/wa-automation/wa-healthcheck.sh >> /tmp/wa-healthcheck.log 2>&1
```

检查项：
1. SSH 隧道是否存活（CDP 端口可达）
2. Worker 进程是否运行
3. 如隧道断开 → 自动重建
4. 如 Worker 挂掉 → 自动重启

## 开发

### 添加新账号

```bash
# Windows (AdsPower)
curl -X POST http://127.0.0.1:50325/api/v1/user/create \
  -H "Content-Type: application/json" \
  -d '{"group_id":"xxx","name":"account-02","proxy_config":{...}}'

# Linux (启动新 worker)
node worker.js --user-id=xxx --ws-endpoint=... --ai-url=... --port=8084 &
```

### 自定义 AI 回复

编辑 `ai_engine/prompts.py` 修改系统提示词。

## 常见问题

**Q: WhatsApp 提示 "Couldn't link device"**
A: 确保使用 AdsPower 指纹浏览器（不是裸 Chrome）+ `puppeteer-extra-plugin-stealth`

**Q: 浏览器启动后崩溃**
A: 这是 Linux 无头服务器常见问题。解决方案：使用 Windows 运行 AdsPower，Linux 通过 SSH 隧道连接

**Q: QR 码不显示**
A: 检查 Dashboard → Worker → SSH 隧道 → Windows CDP 全链路是否通畅

## 许可

MIT
