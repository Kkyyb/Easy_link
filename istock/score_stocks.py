#!/usr/bin/env python
"""
Score stock rows in section.json against the query, then rebuild markdown tables
from the original row text.

The LLM is only allowed to return scores and reasons. It must not regenerate
the final markdown tables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path(r"D:\Work\Easy_link\istock\input_data\section.json")
DEFAULT_OUTPUT_DIR = Path(r"D:\Work\Easy_link\istock\output")
DEFAULT_THRESHOLD = 70
PROJECT_DIR = Path(__file__).resolve().parent


@dataclass
class StockRow:
    stock_id: str
    stock_name: str
    stock_info: str
    columns: list[str]


@dataclass
class StockTable:
    header: str
    separator: str
    rows: list[StockRow]
    start_offset: int
    end_offset: int


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_markdown_row(row: str) -> list[str]:
    row = row.strip()
    if not row.startswith("|") or not row.endswith("|"):
        raise ValueError(f"Invalid markdown table row: {row}")
    return [cell.strip() for cell in row.strip("|").split("|")]


def is_separator_row(row: str) -> bool:
    cells = parse_markdown_row(row)
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def extract_stock_id_and_name(stock_cell: str) -> tuple[str, str]:
    match = re.search(r"(.+?)\((\d{6}\.(?:SH|SZ|BJ))\)", stock_cell.strip(), re.I)
    if match:
        return match.group(2).upper(), match.group(1).strip()
    return stock_cell.strip(), stock_cell.strip()


def extract_stock_table(response: str) -> StockTable:
    lines = response.splitlines(keepends=True)
    start = 0
    for index, line in enumerate(lines):
        if line.strip().startswith("#") and "股票概览" in line:
            start = index + 1
            break

    header_index = None
    for index in range(start, len(lines)):
        line = lines[index].strip()
        if not line.startswith("|"):
            continue
        try:
            cells = parse_markdown_row(line)
        except ValueError:
            continue
        if len(cells) >= 4 and cells[:4] == ["分类", "股票名称", "相关业务", "来源"]:
            header_index = index
            break

    if header_index is None:
        raise ValueError("Could not find stock table header: | 分类 | 股票名称 | 相关业务 | 来源 |")
    if header_index + 1 >= len(lines) or not is_separator_row(lines[header_index + 1].strip()):
        raise ValueError("Stock table header is not followed by a markdown separator row.")

    header = lines[header_index].strip()
    separator = lines[header_index + 1].strip()
    rows: list[StockRow] = []
    table_end_index = header_index + 2

    for line_index, line in enumerate(lines[header_index + 2 :], start=header_index + 2):
        raw = line.strip()
        if not raw.startswith("|"):
            if rows:
                break
            continue
        if is_separator_row(raw):
            continue
        cells = parse_markdown_row(raw)
        if len(cells) < 4:
            continue
        stock_id, stock_name = extract_stock_id_and_name(cells[1])
        rows.append(StockRow(stock_id=stock_id, stock_name=stock_name, stock_info=raw, columns=cells))
        table_end_index = line_index + 1

    if not rows:
        raise ValueError("Found stock table header, but no stock rows were parsed.")

    line_offsets = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line)

    start_offset = line_offsets[header_index]
    end_offset = line_offsets[table_end_index] if table_end_index < len(lines) else len(response)

    return StockTable(
        header=header,
        separator=separator,
        rows=rows,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def make_prompt(query: str, rows: list[StockRow]) -> list[dict[str, str]]:
    scoring_rules = """
请按 0-100 分评价每只股票与 query 的相关度：
- 90-100：股票主营业务、核心产品或核心服务与 query 直接一致，是 query 所指产业、产品、技术、应用或主题的核心供给方/运营方/实现方。
- 80-89：股票的重要业务与 query 高度匹配，但可能只覆盖 query 的部分环节、细分方向或关键产品。
- 70-79：股票处于 query 明确相关的上游、下游、配套、材料、零组件、渠道、客户、场景或基础设施环节，对 query 有清晰产业链关系。
- 50-69：股票与 query 有间接关系、潜在受益关系、需求拉动关系或业务延展关系，但不是核心环节。
- 0-49：股票与 query 关系弱、依据不足，或 stock_info 中没有能支撑相关性的内容。

只根据输入 stock_info 评分。不要补充外部事实。不要改写 stock_info。
必须只返回 JSON，不要 Markdown，不要解释性前后缀。
JSON 格式：
{
  "scores": [
    {
      "stock_id": "688200.SH",
      "stock_point": 92,
      "stock_point_reason": "一句话说明评分依据"
    }
  ]
}
"""
    payload = [
        {
            "stock_id": row.stock_id,
            "stock_name": row.stock_name,
            "stock_info": row.stock_info,
        }
        for row in rows
    ]
    return [
        {
            "role": "system",
            "content": "你是严谨的A股产业链相关度评分助手，只输出可解析JSON。",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "query": query,
                    "scoring_rules": scoring_rules,
                    "stocks": payload,
                },
                ensure_ascii=False,
            ),
        },
    ]


def normalize_base_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def call_chat_completions(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    timeout: int,
    max_retries: int,
    use_response_format: bool,
) -> str:
    url = normalize_base_url(base_url)
    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if use_response_format:
        request_body["response_format"] = {"type": "json_object"}
    data = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            return response_data["choices"][0]["message"]["content"]
        except (
            TimeoutError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(2**attempt)

    raise RuntimeError(f"LLM API request failed after {max_retries + 1} attempt(s): {last_error}")


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def validate_scores(raw_scores: Any, rows_by_id: dict[str, StockRow]) -> list[dict[str, Any]]:
    if not isinstance(raw_scores, list):
        raise ValueError("LLM response field 'scores' must be a list.")

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_scores:
        if not isinstance(item, dict):
            continue
        stock_id = str(item.get("stock_id", "")).upper().strip()
        if stock_id not in rows_by_id:
            continue
        try:
            point = int(item.get("stock_point"))
        except (TypeError, ValueError):
            continue
        point = max(0, min(100, point))
        reason = str(item.get("stock_point_reason", "")).strip()
        row = rows_by_id[stock_id]
        results.append(
            {
                "stock_id": row.stock_id,
                "stock_name": row.stock_name,
                "stock_info": row.stock_info,
                "stock_point": point,
                "stock_point_reason": reason,
            }
        )
        seen.add(stock_id)

    missing = sorted(set(rows_by_id) - seen)
    if missing:
        raise ValueError(f"LLM response is missing scores for: {', '.join(missing)}")
    return results


def score_rows_with_llm(args: argparse.Namespace, query: str, rows: list[StockRow]) -> list[dict[str, Any]]:
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not api_key:
        raise RuntimeError("Missing LLM_API_KEY. Set it in environment or .env.")
    if not model:
        raise RuntimeError("Missing LLM_MODEL. Set it in environment or .env.")

    rows_by_id = {row.stock_id: row for row in rows}
    merged: list[dict[str, Any]] = []

    batch_size = len(rows) if args.batch_size <= 0 else args.batch_size
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        messages = make_prompt(query, batch)
        content = call_chat_completions(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=args.temperature,
            timeout=args.timeout,
            max_retries=args.max_retries,
            use_response_format=not args.no_response_format,
        )
        parsed = extract_json_object(content)
        batch_scores = validate_scores(parsed.get("scores"), {row.stock_id: row for row in batch})
        merged.extend(batch_scores)

    if len(merged) != len(rows_by_id):
        raise ValueError("Scored row count does not match parsed row count.")
    return merged


def make_dry_run_scores(rows: list[StockRow]) -> list[dict[str, Any]]:
    return [
        {
            "stock_id": row.stock_id,
            "stock_name": row.stock_name,
            "stock_info": row.stock_info,
            "stock_point": 0,
            "stock_point_reason": "DRY_RUN：仅验证解析和输出拼接，未调用大模型评分。",
        }
        for row in rows
    ]


def split_scores(
    scores: list[dict[str, Any]],
    threshold: int,
    split_mode: str,
    high_ratio: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sorted_scores = sorted(scores, key=lambda item: (-int(item["stock_point"]), item["stock_id"]))
    if split_mode == "adaptive":
        high_count = max(1, round(len(sorted_scores) * high_ratio))
        return sorted_scores[:high_count], sorted_scores[high_count:]
    high = [item for item in sorted_scores if int(item["stock_point"]) >= threshold]
    normal = [item for item in sorted_scores if int(item["stock_point"]) < threshold]
    return high, normal


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_html_table(title: str, header: str, scores: list[dict[str, Any]]) -> str:
    headers = parse_markdown_row(header)
    lines = ["<table>"]
    lines.append(f'  <tr><th colspan="{len(headers)}" align="center">{escape_html(title)}</th></tr>')
    lines.append("  <tr>" + "".join(f"<th>{escape_html(cell)}</th>" for cell in headers) + "</tr>")
    for item in scores:
        cells = parse_markdown_row(item["stock_info"])
        lines.append("  <tr>" + "".join(f"<td>{escape_html(cell)}</td>" for cell in cells) + "</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def build_replacement_table(header: str, high: list[dict[str, Any]], normal: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            build_html_table("高度相关", header, high),
            build_html_table("一般相关", header, normal),
        ]
    )


def replace_response_table(response: str, table: StockTable, replacement: str) -> str:
    if table.end_offset < len(response) and not replacement.endswith(("\n", "\r")):
        replacement += "\n"
    return response[: table.start_offset] + replacement + response[table.end_offset :]


def write_outputs(
    *,
    output_dir: Path,
    input_data: dict[str, Any],
    original_response: str,
    table: StockTable,
    header: str,
    scores: list[dict[str, Any]],
    high: list[dict[str, Any]],
    normal: list[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "stock_scores.json").write_text(
        json.dumps(scores, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    replacement_table = build_replacement_table(header, high, normal)

    output_data = dict(input_data)
    output_data["response"] = replace_response_table(original_response, table, replacement_table)
    (output_dir / "section_scored.json").write_text(
        json.dumps(output_data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score stock table rows against query.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input section.json path.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory.")
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / ".env", help="Optional .env file path.")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="Fixed split threshold.")
    parser.add_argument(
        "--split-mode",
        choices=["threshold", "adaptive"],
        default="threshold",
        help="Use fixed threshold or top-ratio adaptive split.",
    )
    parser.add_argument("--high-ratio", type=float, default=0.5, help="Adaptive high-related ratio.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Rows scored per LLM request. Use 0 or a negative value to score all rows in one request.",
    )
    parser.add_argument("--temperature", type=float, default=0.0, help="LLM temperature.")
    parser.add_argument("--timeout", type=int, default=600, help="HTTP timeout seconds.")
    parser.add_argument("--max-retries", type=int, default=2, help="LLM API retry count.")
    parser.add_argument(
        "--no-response-format",
        action="store_true",
        help="Disable response_format for providers that do not support it.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse and write outputs without LLM call.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)

    data = json.loads(args.input.read_text(encoding="utf-8"))
    query = str(data.get("query", "")).strip()
    response = str(data.get("response", ""))
    if not query:
        raise ValueError("Input JSON missing non-empty 'query'.")
    if not response:
        raise ValueError("Input JSON missing non-empty 'response'.")

    table = extract_stock_table(response)
    header = table.header
    rows = table.rows
    if args.dry_run:
        scores = make_dry_run_scores(rows)
    else:
        scores = score_rows_with_llm(args, query, rows)

    high, normal = split_scores(
        scores=scores,
        threshold=args.threshold,
        split_mode=args.split_mode,
        high_ratio=args.high_ratio,
    )
    write_outputs(
        output_dir=args.output_dir,
        input_data=data,
        original_response=response,
        table=table,
        header=header,
        scores=sorted(scores, key=lambda item: (-int(item["stock_point"]), item["stock_id"])),
        high=high,
        normal=normal,
    )

    print(f"query: {query}")
    print(f"parsed_rows: {len(rows)}")
    print(f"high_related: {len(high)}")
    print(f"normal_related: {len(normal)}")
    print(f"output_json: {args.output_dir / 'stock_scores.json'}")
    print(f"output_section_json: {args.output_dir / 'section_scored.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
