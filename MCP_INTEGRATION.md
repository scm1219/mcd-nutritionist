# MCP 集成说明（麦麦营养师 × 麦当劳 MCP Server）

本文档说明本项目如何真实使用麦当劳 MCP Server：Server 信息、工具清单、
三条核心调用流程与业务价值；文末附录保留开发期的只读探测实录（字段校准证据）。
客户端直连（免安装模式）的配置参考见 `mcp-config.example.json` 与 `SKILL.md` 模式 B。

## 1. Server 信息

| 项目 | 值 |
| --- | --- |
| 端点 | `https://mcp.mcd.cn` |
| 协议 | Streamable HTTP 上的标准 JSON-RPC（`initialize` → `notifications/initialized` → `tools/list` → `tools/call`），无状态、无 session id 要求 |
| 鉴权 | HTTP 头 `Authorization: Bearer <MCD_MCP_TOKEN>`（Token 到 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 申请；本仓库从环境变量或 `.env` 读取，`.env` 已 gitignore） |
| 限流 | 429 限流时客户端指数退避重试（0.5 / 1 / 2 s），耗尽后报清晰限流错误；401 不重试直接报错；请求超时 15 s |
| 在线工具总数 | 35 个（比官方文档多 `query-promotions`、`query-survey-coupon`，探针实测） |
| 返回格式 | 文本：字段说明 + `## Original Response` 分隔 + 原始 JSON；营养工具的 data 为 CSV 风格行文本，由 `mcdu/` 解析 |

客户端实现：`mcdu/mcp_client.py`（单文件，仅依赖 `requests`）。

## 2. 使用的工具清单（15 个启用 + 1 个备查）

| MCP 工具 | 用途 | 所在命令 |
| --- | --- | --- |
| `list-nutrition-foods` | 全量餐品营养表（160 项，热量/蛋白/脂肪/碳水/钠/钙） | `analyze` / `suggest` / `history` / `order draft`（营养表缓存过期时重拉） |
| `order-list` | 历史订单列表（含套餐子项明细） | `history` |
| `query-nearby-stores` | 按城市+关键词搜索可点餐门店（storeCode / beCode 来源） | `order stores` |
| `query-meals` | 门店在售菜单（10 分钟本地缓存） | `order menu` / `order draft` / `suggest --store` |
| `query-meal-detail` | 套餐默认组成（isDefault 选项） | `order draft` |
| `calculate-price` | 实付价试算（单位为分；可带优惠券） | `suggest --store` / `order draft` |
| `delivery-query-addresses` | 外送地址列表 | `order draft`（`--mode delivery` 缺 `--address-id` 时列出供选择） |
| `create-order` | 真实创建订单（写操作，安全门拦截） | `order confirm --yes-place-order` |
| `query-order` | 订单详情 | `order status` |
| `cancel-order` | 取消订单（写操作，不可逆，安全门拦截） | `order cancel --yes-cancel` |
| `query-my-coupons` | 我的券（分页，字符串页码） | `extras coupons` |
| `available-coupons` | 当前可领券 | `extras coupons` |
| `query-my-account` | 账户积分（字段为字符串数字，解析时转数值） | `extras points` |
| `campaign-calendar` | 当日活动日历（specifiedDate=当日） | `extras campaigns` |
| `auto-bind-coupons` | 一键领取全部可领券（写操作，安全门拦截） | `extras claim --yes-claim` |
| `query-store-coupons` | 门店可用券（schema 备查，v1 未启用，见附录） | — |

## 3. 三条核心调用流程（文本时序）

### 流程一：单餐营养分析（`analyze`）

```text
用户 --items "双层吉士汉堡x1,中薯条x1"
  → CLI 解析点单（支持 巨无霸x2 / 2个巨无霸 写法，过 aliases.json 别名表）
  → 营养表 NutritionTable：本地缓存 data/nutrition_cache.json 新鲜则直接用；
    过期且配置了 Token 时 call_tool("list-nutrition-foods") 重拉全量表（160 项）
  → 脚本精确求和：热量/蛋白/脂肪/碳水/钠/钙、供能比、占每日参考百分比、超限警告
  → 双通道输出：人读报告（默认）/ JSON（--json，供 Agent 消费）
  （无 Token 时 analyze 仍可用：退回纯缓存模式）
```

### 流程二：配餐推荐 + 下单闭环（`suggest --store` → `order`）

```text
suggest --max-kcal 600 --store 1450330
  → query-meals 拿真实菜单（10 分钟缓存）→ 本地按热量/蛋白/价格约束组合
  → calculate-price 逐组试算实付价（分→元，可加 --max-price 预算上限）→ 排序输出
order stores --city 上海 --keyword 陆家嘴
  → query-nearby-stores（beType=1, searchType=2）→ storeCode（顺带 beCode）
order draft --store <CODE> --items "<组合>"
  → query-meals 定位点单项 → 套餐加查 query-meal-detail 默认组成
  → calculate-price 算价（可 --coupon 试算优惠）→ 营养预览
  → 草稿落盘 data/draft_<id>.json（30 分钟 TTL）——全程只读工具，绝不下单
order confirm --draft-id <ID> --yes-place-order（用户明确同意后才加旗标）
  → create-order 真实下单 → 追加 data/orders_log.json → 删除草稿 → 输出订单号
```

### 流程三：历史营养回顾（`history`）

```text
history [--days 7] [--detail]
  → call_tool("order-list")（订单只能来自 MCP，无 Token 即业务错误）
  → 展开每单 orderProductList：有 comboItemList 子项取子项（营销套餐名不计营养），
    无子项才用 productName
  → 营养表逐项匹配求和（未知项显式标注、绝不按 0 计入）
  → 汇总统计：单数 / 平均热量 / 单笔最高 / 平均钠 / 供能比 / 最常点 TOP / 未知项警告
```

## 4. 业务价值

- **健康决策辅助**：把 160 项营养原始数据变成"这一餐吃了多少、占每日参考多少、钠有没有超"的可解释报告；配餐推荐让"想吃 600 大卡以内、高蛋白"这类诉求直接得到满足方案的组合，而不是让用户自己心算。
- **闭环点餐**：从健康诉求（推荐）到门店（查店）到算价（草稿试算）再到真实下单（confirm），一条链路打通麦当劳 MCP 的点餐侧工具；所有写操作（下单/取消/领券）都有 `--yes-*` 安全门，草稿 30 分钟 TTL，Agent 可安全代人执行。
- **可被任意 Agent 驱动**：双通道输出（人读 + `--json`）、明确退出码（0 成功 / 1 业务 / 2 参数）、本地缓存降低 MCP 调用压力——这些约定让 CLI 既能人用，也能被 ZCode / Cursor 等 Agent 按 `SKILL.md` 稳定驱动。

## 附录：开发期只读探测实录（字段校准证据）

任务实现中只读探测得到的服务端真实行为，供后续任务（换餐/特调/下单）参考。
探测均为只读（不创建订单），Token 来自 `.env` 的 `MCD_MCP_TOKEN`。

### calculate-price（Task 7 实录，2026-10-10）

**请求**（beType=1 到店取餐，不传 beCode）：

```json
{
  "storeCode": "1450330",
  "beType": 1,
  "orderType": 1,
  "items": [{"productCode": "521799", "quantity": 1}]
}
```

**响应**（菜单最便宜餐品之一：521799 日式鲜磨山葵酱，currentPrice ¥3.0；
traceId `08971d4ed9f77c6b8d23dc95e522ff79`）：

```json
{
  "success": true, "code": 200, "message": "请求成功",
  "data": {
    "productOriginalPrice": 300,
    "productPrice": 300,
    "originalPrice": 300,
    "discount": 0,
    "price": 300,
    "productList": [
      {"productCode": "521799", "productName": "日式鲜磨山葵酱",
       "quantity": 1, "originalSubtotal": 300, "subtotal": 300}
    ],
    "takeWayList": [
      {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
      {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"}
    ]
  }
}
```

要点：

- **实付金额字段是 `data.price`**（计划草案猜的 `payAmount` 不存在）。
  工具描述亦注明"返回的价格字段单位为**分**，展示时需除以 100 转为元"。
- 同级还有 `productPrice`（商品现价）/`originalPrice`（原价）/`discount`
  （优惠金额），单位均为分；`packingPrice`/`deliveryPrice`/`tablewarePrice`
  仅相应场景出现（本次到店取餐响应中没有这些键）。
- 无折扣时 `price` = 菜单 `currentPrice` 之和（组合实录：双层吉士汉堡 ¥23 +
  麦辣鸡翅 ¥15 → `price` 3800 分 = ¥38.00），因此菜单价合计可作预算预筛排序；
  真实达标与否仍以 `calculate-price` 结果为准。
- `takeWayList`（堂食/外带）为到店下单可选项，Task 8+ 创建订单时需回传 `code`。
- 代码消费方：`scripts/suggest.py:_price_yuan`（字段名常量
  `PRICE_AMOUNT_FIELD`），断言锚定在 `tests/test_suggest_store.py::test_price_from_response_uses_real_field`。

### query-store-coupons（schema 备查，Task 7 未启用）

入参 `{storeCode, beType, orderType[, beCode][, reservationDate]}`（与
calculate-price 同样的 beCode 传递规则：beType=1 不传，外送/得来速必传）；
返回 `data[]`：`couponId`（展示/下单用）/`couponCode`/`title`/`products[]`
（`productCode` + `productName`）/`tradeDateTime`（有效期）。后续做"组合可用券"
时，couponId/couponCode 可直接塞进 calculate-price 的 items 条目试算。

### extras 快查工具（Task 10 只读实录，2026-10-10）

**query-my-account** 入参 `{}`；返回 `data` 标量字段集，值多为**字符串数字**：
`availablePoint`（可用积分，如 `"29"`）/`accumulativePoint`/`usedPoint`/
`expiredPoint`/`frozenPoint`/`nextMouthExpirePoint`/`currency`（"麦享会积分"）/
`accountId`。代码消费方：`scripts/extras.py:extract_points`（字符串数字统一转
数值，锚定 `tests/test_extras.py::test_extract_points_parses_numeric_string`）。

**query-my-coupons** 入参 `{"page": "1", "pageSize": "20"}`（字符串页码）；
返回 `data.coupons[]` + 分页键 `totalCount/currentPage/pageSize/totalPages`
（本次账户无券，条目字段未实录——券名按 couponName 探测）。

**available-coupons** 入参 `{}`；返回 `data[]` 直接是列表：
`couponName`/`couponImage`（URL，渲染不展示）/`couponStatus`（"CAN_GET"）/
`label`（"可领取"）。

**campaign-calendar** 入参 `{"specifiedDate": "YYYY-MM-DD"}`；返回
`data.dailyList[]`：`date`/`dateText`（"2026年10月10日 今日"）/`today`（bool）/
`events[]`。事件字段：`activityCode`/`activityTitle`（**实测常为空串**，真实
标题在 `articleDto.title`，内容在 `articleDto.content`）/`price`（字符串，
实测恒 "0"）+ `priceSuffix`（"起"）/`activityTag`/`button.text`（"提醒我"）。
响应含昨日（"往期回顾"）、当日、次日三组。活动标题常带 emoji，Windows GBK
控制台直打有 UnicodeEncodeError 风险（同 Task 4 已记录的 deferred 项）。

**auto-bind-coupons** 写操作（一键领取全部可领券），入参 `{}`；**未实录**
（真实领券后回填返回形状），CLI 侧已用 `--yes-claim` 安全门拦截。
