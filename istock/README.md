# istock

从 `section.json` 中读取 `query` 和 `response`，提取 `response` 里的“股票概览”Markdown 表格，对每一条股票原始行调用大模型评分，然后按相关度输出两张表。

脚本只让大模型返回 `stock_point` 和 `stock_point_reason`。最终表格由本地代码使用原始 `stock_info` 拼接，避免大模型改写表格内容。

## 文件

- `score_stocks.py`：主脚本
- `.env.example`：环境变量示例
- `output/stock_scores.json`：评分明细输出
- `output/section_scored.json`：保持输入 JSON 结构，只替换 `response` 中股票表格后的完整输出

## 配置

在 `.env`中填写你的模型配置：

```text
LLM_API_KEY=你的apikey
LLM_BASE_URL=https://api.openai.com
LLM_MODEL=gpt-4o-mini
```

`LLM_BASE_URL` 支持 OpenAI-compatible 接口。可以填：

```text
https://api.openai.com
https://你的服务商域名/v1
https://你的服务商域名/v1/chat/completions
```

## 运行

在 `D:\Work\Easy_link\istock` 下运行：

```powershell
python .\score_stocks.py
```

或显式指定输入输出：

```powershell
python .\score_stocks.py --input "D:\Work\Easy_link\section.json" --output-dir "D:\Work\Easy_link\istock\output"
```

不调用大模型，仅验证解析和输出拼接：

```powershell
python .\score_stocks.py --dry-run
```

## 分表规则

默认使用固定阈值：

```text
高度相关：stock_point >= 70
一般相关：stock_point < 70
```

评分建议：

- `90-100`：主营业务、核心产品或核心服务与 query 直接一致，是 query 所指产业、产品、技术、应用或主题的核心供给方/运营方/实现方。
- `80-89`：重要业务与 query 高度匹配，但可能只覆盖 query 的部分环节、细分方向或关键产品。
- `70-79`：处于 query 明确相关的上游、下游、配套、材料、零组件、渠道、客户、场景或基础设施环节。
- `50-69`：与 query 有间接关系、潜在受益关系、需求拉动关系或业务延展关系，但不是核心环节。
- `0-49`：关系弱、依据不足，或 `stock_info` 中没有能支撑相关性的内容。

## 表格输出

`section_scored.json` 的 `response` 中使用 HTML 表格。这样可以让第一行标题跨 4 列居中：

```html
<tr><th colspan="4" align="center">高度相关</th></tr>
```

股票内容仍来自原始 `stock_info`，不会由大模型重新生成。

`section_scored.json` 会保留输入 JSON 的其他字段和值，只把 `response` 中原“股票概览”表格替换为上述两张表，`response` 中表格前后的正文不做改写。

也可以用自适应模式，例如前 50% 归为高度相关：

```powershell
python .\score_stocks.py --split-mode adaptive --high-ratio 0.5
```

## 输出 JSON 结构

```json
{
  "stock_id": "688200.SH",
  "stock_name": "华峰测控",
  "stock_info": "| 功率/模拟测试机、SoC 测试机 | 华峰测控(688200.SH) | ... | 公告 |",
  "stock_point": 92,
  "stock_point_reason": "主营半导体自动化测试系统，直接对应半导体测试设备。"
}
```
