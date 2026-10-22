# 本地生活经营调查 Agent

## 项目简介

这是一个面向本地生活运营经营复盘的 Agent Demo。运营通过自然语言提出问题，选择商品范围与经营周期；Agent 自主查询经营数据、根据结果补查，并结合经营知识输出经营结论、重点商品和下一步建议。调查过程与数据依据可在页面中查看。

项目由个人独立完成产品设计，通过 AI Coding 协作实现。

> 示例问题：这期哪些商品表现突出，哪些明显落后？如果下周只能重点做三款，你建议选哪些？

## 核心功能

- **自主调查**：根据经营问题选择查询和知识检索工具，结合返回结果决定是否继续补查。
- **经营数据分析**：查询订单量、播放量、点击率、千次播放订单（OPM）、千次播放成交额（GPM），支持跨期变化、日级走势和商品状态分析。
- **商品比较与筛选**：支持商品排名、中位数对照，以及按多个指标条件筛选目标商品。
- **结果与依据回查**：展示经营结论、关键数据和建议；可查看调查步骤、结构化数据卡片，并点击引用回查数据依据或查看知识来源。

## 技术实现

- **Agent**：OpenAI Agents SDK，单 Root Agent 负责调查规划、工具调用和结果组织。
- **MCP 与数据层**：四个原生只读查询工具 `get_metric_values`、`compare_periods`、`compare_product_set`、`filter_products`，由 `core/data/` 提供确定性数据计算；`oagent_query` 补充状态变化和日级序列查询。
- **RAG**：`knowledge_search` 检索本地经营知识，使用 BGE Embedding、Chroma。
- **交互与服务**：FastAPI + SSE 返回调查进度和结果，Next.js / React 负责输入、展示与证据回查。

主要代码位于 `agents_oagent/`（Agent、MCP 与服务）、`core/`（数据、知识库及共享逻辑）、`demo_ui/`（前端）。

## 本地运行

准备 Python、Node.js/npm，以及可调用当前模型的 OpenAI 兼容 Chat Completions API。当前模型 ID 在 `agents_oagent/model_provider.py` 中定义（`deepseek-v4-flash`）；模型接口须支持该 ID。

**1. 安装依赖**

在仓库根目录执行：

```bash
pip install -r agents_oagent/requirements.txt
cd demo_ui
npm install
cd ..
```

**2. 配置环境变量**

```bash
cp agents_oagent/.env.example agents_oagent/.env.local
cp demo_ui/.env.example demo_ui/.env.local
```

在 `agents_oagent/.env.local` 中填写模型接口地址及密钥：

```dotenv
OAGENT_RELAY_BASE_URL=你的API地址
OAGENT_RELAY_API_KEY=你的API密钥
```

`demo_ui/.env.local` 使用模板默认配置即可。另需设置本地数据服务与 Agent 共用的 `DIAG_TOOLS_API_KEY`：

macOS / Linux / Git Bash：

```bash
export DIAG_TOOLS_API_KEY="your-local-token"
```

Windows PowerShell：

```powershell
$env:DIAG_TOOLS_API_KEY = "your-local-token"
```

**3. 构建知识索引并启动**

```bash
python -m core.knowledge.build_index --rebuild
python dev.py
```

首次构建知识索引可能需要下载 Embedding 模型。启动脚本会依次运行数据服务（8099）、Agent 服务（8327）和前端（3000）。

浏览器访问 **http://localhost:3000**，按 `Ctrl+C` 结束本次启动的服务。

---

本项目使用模拟经营数据，供本地演示与学习交流；未接入真实企业经营系统，也不代表线上业务效果。模型 API 调用可能产生费用。
