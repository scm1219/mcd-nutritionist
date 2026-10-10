# MCP 集成实录笔记（草稿）

任务实现中只读探测得到的服务端真实行为，供后续任务（换餐/特调/下单）参考。
探测均为只读（不创建订单），Token 来自 `.env` 的 `MCD_MCP_TOKEN`。

## calculate-price（Task 7 实录，2026-10-10）

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

## query-store-coupons（schema 备查，Task 7 未启用）

入参 `{storeCode, beType, orderType[, beCode][, reservationDate]}`（与
calculate-price 同样的 beCode 传递规则：beType=1 不传，外送/得来速必传）；
返回 `data[]`：`couponId`（展示/下单用）/`couponCode`/`title`/`products[]`
（`productCode` + `productName`）/`tradeDateTime`（有效期）。后续做"组合可用券"
时，couponId/couponCode 可直接塞进 calculate-price 的 items 条目试算。
