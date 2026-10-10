# MCP 集成说明

本文说明麦咖啡因管家**实际使用**的麦当劳 MCP Server、Tool、调用流程与业务价值，并记录联调过程中实测到的接口行为。

---

## 1. 连接的 MCP Server

| 项目 | 值 |
|---|---|
| Server | 麦当劳中国官方 MCP Server |
| 地址 | `https://mcp.mcd.cn` |
| 协议 | Streamable HTTP |
| 鉴权 | 请求头 `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 协议版本 | `2025-06-18` |
| 实测工具总数 | 35（`tools/list`） |
| 限流 | 官方文档说明 600 次/分钟，超限返回 429 |

Token 从环境变量 `MCD_MCP_TOKEN` 读取，或从被 `.gitignore` 排除的本地 `.env` 读取。**仓库内不存在任何真实 Token**，[`mcp-config.example.json`](mcp-config.example.json) 只使用 `${MCD_MCP_TOKEN}` 占位符。

---

## 2. 实际使用的 Tool 与调用方式

### 2.1 `list-nutrition-foods` —— 餐品营养底表

- **调用参数**：无
- **实测返回**：160 条餐品记录，字段为 `productName, nutritionDescription, energyKj, energyKcal, protein, fat, carbohydrate, sodium, calcium`
- **本项目用途**：作为饮品的营养侧参照，用于"同样提神，换燕麦奶会少多少能量"这类量化对比；同时用于校验菜单品名与营养表品名的一致性
- **关键实测**：返回的 `data` 字段**不是 JSON 数组，而是一段紧凑表格字符串**，形如
  ```
  [160]{productName,nutritionDescription,energyKj,...}:
    猪柳麦满分,null,1288,308,16,16,24,781,213
    大杯鲜萃咖啡,null,54,13,1,0,0,3,8
  ```
  官方文档没有描述这个格式，客户端必须自带解析器（见 `scripts/mcd_client.py::parse_compact_table`）。

### 2.2 `query-meals` —— 门店实时菜单

- **调用参数**：`storeCode`、`orderType=1`（到店取餐）、`beType=1`（到店自提）
- **实测返回**：15 个分类、127 个餐品，其中 `麦咖啡™` 分类 11 项、`饮品` 分类 12 项
- **返回的关键字段**：`currentPrice`（现价）、`originalPrice`（划线价）、`discountType`（优惠类型，如"促销优惠""随单购麦金卡优惠"）、`tags`
- **本项目用途**：饮品清单与实时价格的唯一来源，`drinks` 与 `plan` 两个子命令都基于它
- **实测样例**：`燕麦奶铁` 现价 ¥9.90 / 原价 ¥23.00（随单购麦金卡优惠）、`麦咖啡™美式` ¥11.90 / ¥19.00（限定特惠）

### 2.3 `query-meal-detail` —— 规格与特调结构

- **调用参数**：`storeCode`、`orderType=1`、`beType=1`、`code`（餐品编码）
- **实测返回**：`supportModify: true`，`modification.items` 给出特调组
- **实测样例**（编码 `507409` → `麦咖啡™冰奶铁中杯`）：
  - 特调组 1：标准 / 去冰 / 多冰 / 少冰
  - 特调组 2：不加 / 加（+¥5.00）
- **本项目用途**：把消费级结论落到一杯**可下单的具体规格**上。菜单里的品名是"麦咖啡™奶铁"（不含杯型），详情返回的才是"麦咖啡™冰奶铁中杯"。本项目优先采用详情品名，因为只有它带杯型——而杯型直接决定咖啡因当量（中杯 126mg vs 大杯 189mg）。

### 2.4 `calculate-price` —— 官方核价

- **调用参数**：`storeCode`、`orderType`、`beType`、`items[]`（`productCode` + `quantity`，可选 `couponId`/`couponCode`/`modification`）
- **实测样例**：2 × `507411`（麦咖啡™冰美式中杯）
  ```
  {"productOriginalPrice":3800,"price":2380,"discount":1420,
   "productList":[{"productName":"麦咖啡™冰美式中杯","quantity":2,
                   "originalSubtotal":3800,"subtotal":2380}]}
  ```
  即原价 ¥38.00 → 实付 **¥23.80**，优惠 ¥14.20（金额单位为分）
- **本项目用途**：`每元咖啡因` 这个指标的分母。本项目的立场很明确：**分母必须是官方核价结果，不是菜单标价**，否则算出来的"性价比"是假的。

### 2.5 `order-list` —— 历史订单（同时是门店编码的来源）

- **调用参数**：无
- **实测返回**：近期历史订单，字段含 `storeCode`、`storeName`、`createTime`、`orderStatus`、`realTotalAmount`、`orderProductList[].comboItemList[]`
- **本项目用途**（两处）：
  1. `today` 子命令据此统计当日咖啡因摄入。注意必须**递归到 `comboItemList`**——套餐里的饮品只出现在子项里，只看顶层商品会漏掉。
  2. **作为可用门店编码的可靠来源**（见 §3.2）。

### 2.6 其余只读工具

| Tool | 状态 | 本项目用途 |
|---|---|---|
| `now-time-info` | 实测正常 | 取服务器时间，避免客户端时区/时钟偏差影响"最晚几点前喝完"的判定 |
| `query-my-account` | 实测正常 | 积分账户，评估"积分兑换 vs 现金购买"哪条路更划算 |
| `available-coupons` | 实测正常（返回 6 张可领券） | 判断是否存在能覆盖饮品的券 |
| `query-my-coupons` | 实测正常（当前券包为空） | 判断手上已有券能否用于本单 |
| `campaign-calendar` | 实测正常 | 读取当月活动，识别饮品线上的限时活动 |
| `mall-points-products` | 实测正常（50 项） | 积分商城兑换目录 |
| `query-lottery-info` / `query-my-prizes` | 实测正常 | 积分抽奖的机会成本参照 |

---

## 3. 调用流程

### 3.1 客户端时序

```
启动
  ├─ 读取 MCD_MCP_TOKEN（环境变量优先，其次 .env）
  ├─ initialize            → 协商 protocolVersion / capabilities / clientInfo
  ├─ notifications/initialized（通知，无返回值）
  └─ 按需 tools/call
        └─ 失败且属于可重试错误 → 指数退避重试（0.4s → 0.8s → 1.6s → 3.2s，默认 5 次）
```

实现见 [`scripts/mcd_client.py`](scripts/mcd_client.py)。重试是**内建能力**而不是上层调用者的责任，原因见 §3.3。

### 3.2 门店编码的获取路径（重要设计约束）

拿不到 `storeCode` 就调不了 `query-meals`，也就拿不到实时价格。实测三条路径：

| 路径 | 实测结果 | 本项目是否采用 |
|---|---|---|
| `query-nearby-stores`（`searchType=2` + `city` + `keyword`） | 参数齐全仍返回 `400 缺少参数`，且受 §3.3 的抖动影响 | 否 |
| `delivery-query-addresses` → `delivery-query-stores` | 账号无配送地址时返回 `{"addresses": []}`，无法继续 | 否（有地址的账号可用） |
| `order-list` 提取 `storeCode` | 稳定可用 | **是**（`McdMcpClient.known_store_code()`） |
| 环境变量 `MCD_STORE_CODE` | 稳定可用 | **是**（优先级最高） |

本项目因此把"门店来源"设计成可插拔：优先显式配置，其次从历史订单推断，最后才尝试地理搜索。这也让工具在任何有历史订单的账号上都能开箱即用。

### 3.3 实测行为记录：间歇性 `400 缺少参数`

联调中观察到的原始时间线（同一参数、同一门店、同一 Token）：

| 时间 | 工具 | 结果 |
|---|---|---|
| 16:17:35 | `query-meals`（真实 storeCode 1950891） | 失败 `400 缺少参数` |
| 16:17:47 | `query-meals`（参数完全相同） | **成功** |
| 16:18:12 | `query-meal-detail`（code=507409） | **成功** |
| 16:21:17 | `query-meal-detail` × 5 + `calculate-price` | 全部失败 `400 缺少参数` |
| 16:21:58 | `calculate-price`（同一参数） | **成功** |

已排除的原因：

- **不是参数写错**：`storeCode`、`orderType`、`beType`、`code` 均按官方文档与 `tools/list` 的 `inputSchema` 传入，且同一参数在几分钟后成功。
- **不是缺少会话握手**：加上 `initialize` + `notifications/initialized` 后仍复现；不加握手也能成功。
- **不是权限问题**：同一 Token 下账号类工具（`query-my-account`、`order-list`、`available-coupons`）全程稳定。

结论：**上游把"参数不合法"与"服务端瞬时故障"折叠成了同一个错误码**，调用方无法区分，只能按可重试错误处理。本项目因此对所有 `缺少参数` / `系统繁忙` / `请求超时` / `服务异常` 响应做有上限的指数退避重试，并在最终失败时抛出带 `code`/`message` 的明确异常，而不是静默返回空数据。

对下游的实际意义：一个不做重试的 Skill 在演示时会有肉眼可见的概率性失败；这是本项目在客户端层额外加固的原因。

---

## 4. 业务价值

### 4.1 补上官方数据缺口

`list-nutrition-foods` 提供了 6 个营养维度，却没有咖啡因。这不是数据遗漏，而是"营养表"与"功能成分表"的口径差异——但对每天下午三点想喝一杯的人来说，**咖啡因恰恰是唯一重要的那个数字**。本项目用集中、可审计、可覆盖的模型填上了这个位置，并且严格区分官方数据与模型估算。

### 4.2 把静态菜单变成时间函数

同一杯中杯美式，早上 9 点喝和晚上 8 点喝，价值完全不同。本项目让结论随时间变化：

| 时间 | 中杯美式（126mg）的结论 |
|---|---|
| 09:00 | 现在还能喝，最晚 13:08 前 |
| 20:00 | 已过截止时刻，建议改喝 12mg 的浓浓黑巧 |

这带来两个别处没有的能力：**时间倒推的决策**（`plan`）与**每日累计的口径**（`today`）。

### 4.3 让"性价比"落到可信的分母上

`每元咖啡因` 必须用 `calculate-price` 的官方实付价计算。实测中 2 杯中杯美式的菜单价是 ¥38.00，实付 ¥23.80——如果用菜单价当分母，排序结论会直接出错。同理，同一杯在"随单购麦金卡"下是 ¥9.90、原价 ¥23.00，本项目展示的是**官方返回的现价与优惠类型**，不自己编造折扣。

### 4.4 与门店实时状态绑定

饮品是否在售、规格如何、特调选项有哪些，全部来自 `query-meals` 与 `query-meal-detail`，而不是硬编码菜单。门店缺货、季节下架、限定上新都会自然反映在输出里。

---

## 5. 复现方式

```bash
# 采集数据快照（需要 Token，一次即可）
python scripts/fetch_snapshot.py --store 1950891

# 离线运行全部命令
python scripts/coach.py --offline drinks
python scripts/coach.py --offline cup "冰美式大杯"
python scripts/coach.py --offline plan --now "2026-10-10T20:00"

# 回归测试
python -m unittest discover -s tests -v
```

`data/` 下的快照均由 `fetch_snapshot.py` 通过上述只读工具生成，不含任何 Token 或个人信息。
