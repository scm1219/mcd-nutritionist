# 麦麦营养师（mcd-nutritionist）

基于**麦当劳 MCP Server** 的健康点餐助手：把 160 项餐品营养数据、门店菜单、
真实算价与下单能力串成一条"吃得明白"的链路。命令行工具 + Agent Skill 双形态，
既可以直接人用，也可以交给 ZCode / Cursor / WorkBuddy 等 Agent 驱动。

<!-- TODO: screenshot -->

## 项目简介

爱吃麦当劳，也在意热量和钠？麦麦营养师帮你**点得明白、点得对、下得单**：

| 三大功能 | 命令 | 一句话说明 |
| --- | --- | --- |
| 一餐营养分析（含历史回顾） | `analyze` / `history` | 这顿吃了多少热量、蛋白质、钠，占每日参考多少；最近吃麦当劳频率与营养统计 |
| 健康配餐推荐 | `suggest` | 按"热量上限 / 蛋白下限 / 预算上限"约束推荐组合，门店模式用真实菜单+实付价 |
| 门店点餐闭环 | `order` | 查门店 → 菜单 → 算价草稿（绝不下单）→ 用户确认后真实下单/查单/取消 |

另有 `extras` 快查彩蛋：我的券、积分、当日活动日历、一键领券。

所有营养计算由脚本精确求和，**零 LLM 心算**；匹配不到的餐品显式标注，绝不按 0 计。

## 安装

要求 Python 3.10+。

```bash
git clone <本仓库地址>
cd mcd-nutritionist
pip install -r requirements.txt
cp .env.example .env   # Windows: copy .env.example .env
```

编辑 `.env`，填入从 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 申请的 Token：

```text
MCD_MCP_TOKEN=<你的 Token>
```

说明：无 Token 时 `analyze` / `suggest`（基础模式）仍可用（读本地营养缓存
`data/nutrition_cache.json`）；历史订单、门店、下单、券类命令需要 Token。
运行测试：`pip install -r requirements-dev.txt && python -m pytest tests/ -v`。

## 快速开始

以下输出为真实运行结果节选，仅作示例（数据可能随菜单变化）。

### 1. 一餐营养分析

```bash
python cli.py analyze --items "双层吉士汉堡x1,中薯条x1"
```

```text
麦当劳营养报告

餐品         数量  热量(kcal)
双层吉士汉堡  x1  429
中薯条       x1  289

合计
  热量 718 kcal | 蛋白质 31 g | 脂肪 34 g | 碳水 69 g
  钠 1182 mg | 钙 250 mg

供能比：蛋白 17.6% | 脂肪 43.3% | 碳水 39.1%
每日参考占比：热量 35.9% | 钠 59.1%
```

<!-- TODO: screenshot -->

### 2. 健康配餐推荐

```bash
python cli.py suggest --max-kcal 600 --min-protein 25
```

```text
配餐建议 TOP3

[1] 蛋白质 37 g | 热量 589 kcal
    原味板烧鸡腿炒双蛋堡 + 麦乐鸡4块
    脂肪 31 g | 碳水 41 g | 钠 1052 mg | 钙 123 mg
[2] 蛋白质 37 g | 热量 592 kcal
    双层原味板烧鸡腿麦满分 + 麦辣鸡翅-2块 + 大杯鲜萃咖啡
    ...
```

加 `--store <storeCode>` 切换门店模式：用该店真实菜单组合，并经
`calculate-price` 给出**实付价**（可加 `--max-price 40` 控预算）。

### 3. 历史订单营养回顾

```bash
python cli.py history
```

```text
历史订单营养回顾（2 单）

  平均热量 280.5 kcal | 单笔最高 561 kcal | 平均钠 119 mg
  供能比：蛋白 7.9% | 脂肪 29.2% | 碳水 62.8%
  最常点 TOP3：泷情蜜意麦旋风（无小卡） x2、草莓麦旋风 x1、奥利奥麦旋风 x1
  ⚠️ 2 份未知营养项未计入统计（如：泷情蜜意麦旋风（无小卡），可在 aliases.json 添加别名）
```

`--days 7` 只看最近一周，`--detail` 展开每单明细。

### 4. 门店点餐闭环（草稿绝不下单，确认需安全门）

```bash
python cli.py order stores --city 上海 --keyword 陆家嘴
```

```text
可点餐门店（5 家）
[1] 麦当劳上海陆家嘴明珠环餐厅（1450127）
    地址：世纪大道55号2F-1室
    距离约 75 米
[2] 麦当劳上海正大广场餐厅（1450579）
    ...
```

拿到 `storeCode` 后继续：

```bash
python cli.py order menu   --store 1450127                       # 查菜单
python cli.py order draft  --store 1450127 --items "双层吉士汉堡x1"   # 算价+营养草稿
python cli.py order confirm --draft-id <draft_id> --yes-place-order  # 用户明确同意后才真实下单
python cli.py order status --order-id <order_id>                  # 查单
```

<!-- TODO: screenshot -->

全部子命令支持 `--json` 机器可读输出；完整命令表见 [`SKILL.md`](SKILL.md)。

## 作为 Agent Skill 使用

本仓库的 [`SKILL.md`](SKILL.md) 是 Agent 技能说明，分两种模式：**模式 A**
（本地 CLI，功能完整）与**模式 B**（MCP 连接器直连，免安装只读）。

- **ZCode**：克隆本仓库后让 Agent 按 `SKILL.md` 模式 A 驱动本地 CLI，从营养分析到下单全流程可用。
- **Cursor**：按 [`mcp-config.example.json`](mcp-config.example.json) 配置麦当劳 MCP 连接器（Streamable HTTP + Bearer Token），并将 `SKILL.md` 纳入项目上下文，Agent 即可按模式 B 只读调用。
- **WorkBuddy**：在已配置麦当劳 MCP 连接器的会话中使用 `SKILL.md` 模式 B，配合内置 Top40 营养速查表做免安装的营养估算与回顾。

MCP Server 的端点、鉴权、工具清单与三条核心调用流程详见
[`MCP_INTEGRATION.md`](MCP_INTEGRATION.md)。

## 目标用户

- 减脂 / 增肌人群：按热量与蛋白质预算点餐
- 健身党：训练日碳水补给、钠摄入控制
- 麦门爱好者：想吃就吃，但想知道这顿吃了什么

## 免责声明

本项目为麦当劳程序员节创意开发大赛参赛作品，由参赛者独立开发，**非麦当劳官方产品**。
项目输出（营养数据、配餐建议、价格试算等）仅供参考，不构成医疗、营养或其他专业建议；
餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。下单、支付等操作请用户自行确认，
本项目不代付、不代点支付链接。

## 社区贡献：别名表

餐品叫法因人而异，欢迎通过 [`aliases.json`](aliases.json) 补充别名
（键 = 习惯叫法，值 = 营养表标准名），欢迎 PR：

```json
{
  "中薯": "中薯条",
  "大薯": "大薯条",
  "板烧": "原味板烧鸡腿堡",
  "麦辣鸡": "麦辣鸡腿堡"
}
```

## 许可证

[MIT](LICENSE)
