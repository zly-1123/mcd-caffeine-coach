"""麦当劳 MCP Server（Streamable HTTP）最小客户端。

纯标准库实现（urllib），无需安装任何第三方依赖。

为什么需要这一层：官方 MCP Server 在实测中会对点餐/菜单类工具**间歇性**返回
`success=false, code=400, message="缺少参数"`，即使参数完全正确——同样的调用在
几分钟内会先失败后成功。因此本客户端把"重试"做成了内建能力，而不是让上层调用者
自己处理。这是本项目在实测中得到的工程结论，详见 MCP_INTEGRATION.md。

用法：
    client = McdMcpClient.from_env()
    data = client.call_tool("query-meals", {"storeCode": "1950891", "orderType": 1, "beType": 1})
"""

from __future__ import annotations

import json
import os
import random
import time as time_mod
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_URL = "https://mcp.mcd.cn"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# 上游把"请求参数不合法"和"服务端瞬时故障"都折叠成了同一个 400/缺少参数，
# 因此只能按可重试错误处理，并对同一个请求做有上限的退避重试。
RETRYABLE_MESSAGES = ("缺少参数", "系统繁忙", "请求超时", "服务异常")


class McdMcpError(RuntimeError):
    """MCP 调用最终失败时抛出。"""


def _load_dotenv(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def parse_compact_table(text: str) -> List[Dict[str, str]]:
    """解析官方返回的紧凑表格字符串，形如：

        [160]{productName,energyKcal,...}:
          猪柳麦满分,null,308,16,...

    这类"字符串里塞表格"的返回在 list-nutrition-foods 上实测存在，
    因此这里提供一个容错解析器。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header_match = None
    for ln in lines:
        header_match = None
        stripped = ln.strip()
        if stripped.startswith("[") and "]{" in stripped and "}" in stripped:
            fields_part = stripped.split("]{", 1)[1].rsplit("}", 1)[0]
            header_match = [f.strip() for f in fields_part.split(",") if f.strip()]
            break
    if not header_match:
        return []

    rows: List[Dict[str, str]] = []
    started = False
    for ln in lines:
        stripped = ln.strip()
        if not started:
            if stripped.startswith("[") and "]" in stripped:
                started = True
            continue
        cells = [c.strip() for c in stripped.split(",")]
        if len(cells) < 2:
            continue
        row = {}
        for idx, field_name in enumerate(header_match):
            row[field_name] = cells[idx] if idx < len(cells) else ""
        rows.append(row)
    return rows


class McdMcpClient:
    def __init__(self, token: str, url: str = DEFAULT_URL, timeout: float = 40.0):
        if not token:
            raise ValueError("缺少 MCP Token：请设置环境变量 MCD_MCP_TOKEN 或写入 .env")
        self.token = token
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._request_id = 0
        self._initialized = False

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_env(cls, env_path: Optional[str] = None) -> "McdMcpClient":
        env_path = env_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
        dotenv = _load_dotenv(env_path)
        token = os.environ.get("MCD_MCP_TOKEN") or dotenv.get("MCD_MCP_TOKEN", "")
        url = os.environ.get("MCD_MCP_URL") or dotenv.get("MCD_MCP_URL") or DEFAULT_URL
        return cls(token=token, url=url)

    # ---------------------------------------------------------------- HTTP
    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:") :].strip())
            return {}
        return json.loads(raw)

    # ------------------------------------------------------------ 协议动作
    def initialize(self, force: bool = False) -> Dict[str, Any]:
        if self._initialized and not force:
            return {"cached": True}
        self._request_id += 1
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcd-caffeine-coach", "version": "1.0.0"},
                },
            }
        )
        # 标准流程要求随后发送 initialized 通知；实测可提升点餐类工具的稳定性。
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        self._initialized = True
        return result

    def list_tools(self) -> List[Dict[str, Any]]:
        self.initialize()
        self._request_id += 1
        result = self._post(
            {"jsonrpc": "2.0", "id": self._request_id, "method": "tools/list", "params": {}}
        )
        return result.get("result", {}).get("tools", [])

    def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        retries: int = 5,
        base_delay: float = 0.4,
        raise_on_failure: bool = True,
    ) -> Dict[str, Any]:
        """调用工具并返回 structuredContent（若无则回退到解析文本）。"""
        self.initialize()
        arguments = arguments or {}
        last: Dict[str, Any] = {}

        for attempt in range(1, retries + 1):
            self._request_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            try:
                response = self._post(payload)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = {"success": False, "message": f"传输层错误: {exc}"}
                if attempt < retries:
                    time_mod.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.15))
                    continue
                break

            result = response.get("result", {}) if isinstance(response, dict) else {}
            structured = result.get("structuredContent")
            if structured is None and result.get("content"):
                text = (result.get("content") or [{}])[0].get("text", "")
                structured = {"success": None, "raw_text": text}
            last = structured or {}

            ok = last.get("success")
            message = str(last.get("message", ""))
            retryable = ok is False and any(tag in message for tag in RETRYABLE_MESSAGES)

            if ok or not retryable:
                break
            if attempt < retries:
                time_mod.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.15))

        if raise_on_failure and last.get("success") is False:
            raise McdMcpError(
                f"工具 {name} 调用失败（已重试 {retries} 次）: "
                f"code={last.get('code')} message={last.get('message')}"
            )
        return last

    # ------------------------------------------------------------ 业务封装
    def nutrition_table(self) -> List[Dict[str, str]]:
        """list-nutrition-foods：返回 160 条餐品营养记录。"""
        data = self.call_tool("list-nutrition-foods")
        payload = data.get("data")
        if isinstance(payload, list):
            return payload
        # 实测该工具的 data 字段是一个"字符串里塞表格"的紧凑格式，
        # 形如 "[160]{productName,energyKcal,...}:\n  猪柳麦满分,null,308,..."
        text = payload if isinstance(payload, str) else (data.get("raw_text") or "")
        return parse_compact_table(text)

    def menu(self, store_code: str, order_type: int = 1, be_type: int = 1) -> Dict[str, Any]:
        return self.call_tool(
            "query-meals",
            {"storeCode": store_code, "orderType": order_type, "beType": be_type},
        )

    def meal_detail(self, code: str, store_code: str, order_type: int = 1, be_type: int = 1) -> Dict[str, Any]:
        return self.call_tool(
            "query-meal-detail",
            {"storeCode": store_code, "orderType": order_type, "beType": be_type, "code": code},
        )

    def orders(self) -> Dict[str, Any]:
        return self.call_tool("order-list")

    def price(self, items: List[Dict[str, Any]], store_code: str, order_type: int = 1, be_type: int = 1) -> Dict[str, Any]:
        return self.call_tool(
            "calculate-price",
            {
                "storeCode": store_code,
                "orderType": order_type,
                "beType": be_type,
                "items": items,
            },
        )

    def known_store_code(self) -> Optional[str]:
        """从历史订单里推断一个可用门店编码（实测比 query-nearby-stores 更可靠）。"""
        env_code = os.environ.get("MCD_STORE_CODE")
        if env_code:
            return env_code
        data = self.orders()
        for order in (data.get("data", {}) or {}).get("list", []) or []:
            code = order.get("storeCode")
            if code:
                return str(code)
        return None
