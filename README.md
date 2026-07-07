# AIOps-Bastion

基于 MCP 与 RAG 的多节点智能运维堡垒机。

## 这是什么

一个跑在家庭内网 NAS(2C4G)上的 SRE Agent:接收 Uptime Kuma 告警或用户对话,自动 SSH 到 1~3 台被纳管节点排查故障,根因定位后经人工审批(HITL)执行修复,产出 Markdown 调查报告推送 Telegram。

**项目定位:求职作品集**(非生产系统)。精致度是卖点,但每个决策须能讲清取舍。

## 文档地图

| 文档 | 内容 |
|:---|:---|
| [docs/DETAILED_DESIGN_v1.2.md](docs/DETAILED_DESIGN_v1.2.md) | 详细设计 v1.3(spike 验证后修订) |
| [docs/TRADEOFFS_v1.2.md](docs/TRADEOFFS_v1.2.md) | 设计取舍说明(面试答辩用) |
| [docs/SPIKE_REPORT.md](docs/SPIKE_REPORT.md) | 4 项技术假设验证报告 |
| [spike/](spike/) | 最小验证脚本(01-04) |

## 快速开始

```bash
# 1. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 2. 安装 (含 dev 依赖)
pip install -e ".[dev]"

# 3. 跑测试
pytest tests/ -v

# 4. lint
ruff check src tests
```

## 部署

详见 [docker-compose.yml](docker-compose.yml) 与设计 §7.4。首启经 WebUI 设 Master Password + 录入凭证。

## 技术栈

详见设计 §2.3。Python 3.11+(实测 3.14 可用),LangGraph 1.2.7,DeepSeek-V4-Pro(经 OpenAI 兼容端点)。
