# 麦麦营养师（mcd-nutritionist）

帮用户在麦当劳"点得明白"：一餐营养分析、按热量/蛋白质推荐配餐、查门店菜单、
算价草稿与真实下单、券/积分/活动速查。本文件是 Agent 使用本项目的技能说明，
有 **A / B 两种模式**，按客户端能力选其一：

- **模式 A（推荐，功能完整）**：能运行本地命令时，用仓库里的 CLI——
  营养分析、推荐、下单全流程可用。
- **模式 B（免安装，只读）**：客户端已配置麦当劳 MCP 连接器时，Agent 直接调用
  只读 MCP 工具，配合下方 Top40 速查表做营养估算；不能下单。

---

## 模式 A：本地 CLI

### 环境要求

- Python **3.10+**；安装依赖：`pip install -r requirements.txt`（仅 `requests`）
- 配置 Token：复制 `.env.example` 为 `.env`，填 `MCD_MCP_TOKEN=<token>`
  （Token 到 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 申请）
- 工作目录：仓库根（`cli.py` 所在目录）
- 无 Token 时：`analyze` / `suggest` 仍可用（读本地缓存
  `data/nutrition_cache.json`）；订单/门店/券类命令需要 Token

### 命令速查表（全部子命令，均可加 `--json` 输出机器可读格式）

```text
# 营养分析
python cli.py analyze --items "双层吉士汉堡x1,中薯条x1" [--daily-kcal 2000] [--daily-sodium 2000] [--json]
python cli.py history [--days 7] [--detail] [--json]
python cli.py suggest --max-kcal 600 [--min-protein 25] [--top 3] [--json]
python cli.py suggest --max-kcal 600 --store 1450330 [--min-protein 25] [--top 3] [--max-price 40] [--mode pickup|delivery] [--be-code CODE] [--json]

# 门店与下单
python cli.py order stores --city 上海 --keyword 陆家嘴 [--json]
python cli.py order menu --store 1450330 [--mode pickup|delivery] [--be-code CODE] [--json]
python cli.py order draft --store 1450330 --items "双层吉士汉堡x1,中薯条x1" [--coupon ID] [--address-id ID] [--mode pickup|delivery] [--be-code CODE] [--no-tableware] [--json]
python cli.py order confirm --draft-id ID --yes-place-order [--json]
python cli.py order status --order-id ID [--json]
python cli.py order cancel --order-id ID --reason 1 --yes-cancel [--json]

# 券 / 积分 / 活动
python cli.py extras coupons [--json]
python cli.py extras points [--json]
python cli.py extras campaigns [--json]
python cli.py extras claim --yes-claim [--json]
```

要点：点单写法支持 `巨无霸x2` / `2个巨无霸`，逗号分隔多项；`--mode delivery`
（麦乐送）需要 `--be-code`（来自 `order stores` 输出）与 `--address-id`；
`--daily-kcal` / `--daily-sodium` 是每日参考基准（默认 2000 kcal / 2000 mg 钠）。

### 每个命令的触发意图与输出解读

| 命令 | 触发意图（用户说…） | 输出解读 |
| --- | --- | --- |
| `analyze` | "这顿吃了多少热量/钠？" | 每项营养 + 整餐合计（热量/蛋白/脂肪/碳水/钠/钙）、供能比、占每日参考百分比、超限警告；未匹配的餐品列在 unknown，绝不按 0 计 |
| `history` | "我最近吃麦当劳吃得多吗？" | 历史订单的营养汇总统计；`--days N` 只看最近 N 天，`--detail` 展开每单明细 |
| `suggest` | "想吃 600 大卡以内 / 高蛋白的" | 满足热量/蛋白约束的配餐组合（主餐必选 + 小食/饮品可选）；带 `--store` 时用真实菜单组合并给实付价（可加 `--max-price`） |
| `order stores` | "附近有什么麦当劳？" | 门店列表：`storeCode`（后续命令用）、门店名、地址、`beCode`（麦乐送用） |
| `order menu` | "这家店有什么？" | 门店菜单（分类、餐品名、`productCode`、现价）；菜单缓存 10 分钟 |
| `order draft` | "帮我点这单 / 先算多少钱" | 只读算价（含优惠券试算）+ 营养预览 + 生成 `draft_id`；**不会下单**，草稿 30 分钟内有效 |
| `order confirm` | 用户明确同意下单后 | 真实创建订单：订单号 + 支付相关字段（原样打印）+ 下一步查询命令 |
| `order status` | "我的订单到哪一步了？" | 订单状态、支付状态、金额、时间等字段 |
| `order cancel` | 用户明确要求取消订单 | 取消订单（不可逆）；`--reason` 取值 1=改主意 2=重复下单 3=点错/多点/少点 4=地址/电话填错 5=送达时间选错 -1=其它 |
| `extras coupons` | "我有什么券 / 有什么券可领？" | 我的券与可领券合并列表（只读） |
| `extras points` | "我有多少积分？" | 可用/累计/已用/将过期积分（只读） |
| `extras campaigns` | "今天有什么活动？" | 当日活动日历（只读） |
| `extras claim` | 用户明确同意领券后 | 一键领取全部可领券（真实写操作） |

### 对话剧本（典型全程）

```text
用户：想吃 600 大卡以内，有什么推荐？

Agent：python cli.py suggest --max-kcal 600 --min-protein 25
       → 挑 1-2 个组合复述给用户（热量/蛋白/价格/含什么）

用户：第一个不错，帮我下单吧。        ← 用户表达下单意向

Agent：python cli.py order stores --city <城市> --keyword <位置>
       → 拿 storeCode（顺带记下 beCode，麦乐送要用）
       python cli.py order menu --store <storeCode>
       → 确认餐品在售（不在售就换 suggest --store 重新推荐）
       python cli.py order draft --store <storeCode> --items "<组合>"
       → 把草稿的价格/营养/门店复述给用户，等用户明确同意

用户：确认，下单吧。                  ← 明确同意（安全门）

Agent：python cli.py order confirm --draft-id <id> --yes-place-order
       → 订单号 + 支付相关字段

Agent：把支付链接原样发给用户，请用户自行点击完成支付。
       Agent 绝不打开/代点/代付支付链接。
```

### 安全规则（必须遵守）

1. **写操作三道安全门**：`order confirm`（真实下单）、`order cancel`（不可逆取消）、
   `extras claim`（真实领券）必须等用户**明确同意**后才加对应 `--yes-*` 旗标执行；
   不加旗标时命令只打印将要执行的动作并退出（退出码 1），这是预期行为不是故障。
2. **草稿 30 分钟 TTL**：`order draft` 之后尽快 confirm；过期草稿会被拒绝，需重新 draft。
3. **支付链接只给用户手动点击**：Agent 不得打开、截取或代替用户完成支付。
4. `order draft` 全程只调只读工具（算价不下单）；真实下单只发生在 `order confirm`。

---

## 模式 B：MCP 连接器直连（只读）

适用：客户端（Claude Desktop / Cursor 等支持 MCP 的环境）已配置麦当劳 MCP
连接器，Agent 可直接调用 MCP 工具。此模式**不依赖本仓库代码**。

### 前置：配置连接器

参考仓库里的 `mcp-config.example.json`：

- 端点：`https://mcp.mcd.cn`
- 鉴权：HTTP 头 `Authorization: Bearer <MCD_MCP_TOKEN>`（Token 到
  open.mcd.cn/mcp 申请）

各客户端的连接器配置字段名略有差异（server 名、type、headers 结构等），
按所用客户端的 MCP / Streamable HTTP 连接器文档填入同样的 URL 与鉴权头即可。

### Agent 直接调用 MCP 工具（只读三件套）

| MCP 工具 | 用途 | 调用要点 |
| --- | --- | --- |
| `list-nutrition-foods` | 全量餐品营养表 | 入参 `{}`；返回 CSV 文本（一行头 `[条数]{字段列表}:` + 数据行：名称, 描述, 热量kJ, kcal, 蛋白, 脂肪, 碳水, 钠, 钙），Agent 自行解析 |
| `order-list` | 历史订单 | 入参 `{}`（或分页参数）；用于"最近吃了什么"的营养回顾 |
| `query-meals` | 门店菜单 | 入参含 `storeCode`（与取餐方式参数）；用于查门店在售餐品 |

流程建议：用户问"这顿吃了多少热量"→ 先在下方速查表里按名称找，找不到或需要
全量精确数据时再实时调 `list-nutrition-foods`；问"最近吃得怎么样"→ `order-list`
拿订单后逐项对照速查表汇总。

### Top40 营养速查表

按"主餐/小食/饮品"三类采样（各类蛋白靠前者），数值为每份；由
`python tools/gen_quick_table.py --inject SKILL.md` 生成并注入本区。

<!-- QUICK-TABLE-START -->
| 名称 | kcal | 蛋白(g) | 脂肪(g) | 碳水(g) | 钠(mg) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 芝士双层安格斯厚牛堡 | 1003 | 58 | 65 | 46 | 1373 |
| 培根双层安格斯厚牛堡 | 987 | 55 | 64 | 46 | 1191 |
| 培根安格斯厚牛堡 | 707 | 34 | 44 | 43 | 1037 |
| 芝士安格斯厚牛堡 | 696 | 32 | 44 | 43 | 897 |
| 双层猪柳蛋麦满分 | 513 | 29 | 32 | 26 | 1210 |
| 不素之霸双层牛堡 | 493 | 28 | 27 | 34 | 1012 |
| 双层深海鳕鱼堡 | 485 | 28 | 21 | 46 | 917 |
| 双层吉士汉堡 | 429 | 27 | 22 | 31 | 1017 |
| 原味板烧鸡腿炒双蛋堡 | 419 | 27 | 21 | 30 | 715 |
| 猪柳炒双蛋堡 | 437 | 25 | 24 | 29 | 705 |
| 麦辣鸡腿汉堡 | 485 | 24 | 24 | 42 | 1208 |
| 培根蔬萃双层牛堡 | 442 | 24 | 23 | 34 | 728 |
| 猪柳蛋麦满分 | 387 | 23 | 21 | 25 | 846 |
| 双层原味板烧鸡腿麦满分 | 355 | 23 | 17 | 27 | 984 |
| 麦辣鸡翅-2块 | 224 | 13 | 15 | 9 | 537 |
| 麦乐鸡5块 | 213 | 12 | 12 | 13 | 422 |
| 麦乐鸡4块 | 170 | 10 | 10 | 11 | 337 |
| 大薯条 | 379 | 6 | 16 | 50 | 216 |
| 中薯条 | 289 | 4 | 12 | 38 | 165 |
| 大杯玉米杯 | 87 | 4 | 1 | 12 | 2 |
| 脆脆薯条 | 279 | 4 | 14 | 32 | 540 |
| 小薯条 | 210 | 3 | 9 | 28 | 120 |
| 迷你薯条 | 106 | 2 | 4 | 14 | 61 |
| 小杯玉米杯 | 53 | 2 | 1 | 7 | 1 |
| 香芋派 | 232 | 2 | 12 | 28 | 159 |
| 菠萝派 | 221 | 2 | 11 | 27 | 147 |
| 冰牛奶大杯 | 269 | 15 | 13 | 23 | 154 |
| 热奶铁大杯 | 249 | 14 | 11 | 22 | 134 |
| 热浓浓抹茶牛奶大杯 | 333 | 14 | 12 | 41 | 142 |
| 热牛奶大杯 | 240 | 13 | 11 | 21 | 137 |
| 冰牛奶中杯 | 224 | 13 | 11 | 19 | 128 |
| 卡布奇诺大杯 | 223 | 13 | 10 | 20 | 120 |
| 热奶铁中杯 | 186 | 11 | 8 | 17 | 99 |
| 热浓浓抹茶牛奶中杯 | 238 | 11 | 8 | 29 | 104 |
| 热牛奶中杯 | 176 | 10 | 8 | 15 | 101 |
| 冰奶铁大杯 | 162 | 9 | 7 | 15 | 85 |
| 卡布奇诺中杯 | 167 | 9 | 7 | 15 | 88 |
| 冰浓浓抹茶牛奶大杯 | 240 | 9 | 7 | 33 | 89 |
| 浓浓抹茶雪冰大杯 | 339 | 8 | 12 | 48 | 194 |
| 板烧鸡腿堡 | 391 | 23 | 17 | 35 | 1041 |
<!-- QUICK-TABLE-END -->

### 模式 B 限制声明

- **无下单**：连接器模式只使用只读工具（`list-nutrition-foods` / `order-list` /
  `query-meals`），不能创建/取消订单、不能领券；需要下单请改用模式 A。
- **LLM 近似计算**：营养合计由 Agent 基于速查表推算，数值可能有出入；
  精确分析请用模式 A 的 `analyze`（脚本精确求和）。
- **无缓存**：模式 B 每次实时调用 MCP 工具；速查表只覆盖 40 个常见餐品，
  未收录的餐品请实时调 `list-nutrition-foods` 查询。
