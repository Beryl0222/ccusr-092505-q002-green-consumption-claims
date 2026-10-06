# 绿色消费凭证争议台

本项目提供绿色消费凭证争议台的领域核心与事件交换契约：接收商家版本化承诺（含适用商品与适用时间）、消费者授权的订单证据、回收交接、低碳服务凭证与协商记录，区分事实材料、商家自证与调解结论，支撑立案、凭证受理、调解与补偿整改确认，并保证全过程可解释。

## 目录

- `contracts/domain.schema.json`：领域事件信封和已登记类型。
- `data/sample.json`：中文联调样例。
- `src/green_consumption_claims/contracts.py`：不依赖第三方包的契约校验器。
- `src/green_consumption_claims/domain.py`：领域模型、绿色行动计入与个人信息脱敏。
- `src/green_consumption_claims/store.py`：JSONL 事件存储，重启后按序重放。
- `src/green_consumption_claims/platform.py`：受理、冻结、调解与解释的领域核心。
- `tests/`：契约边界与领域规则检查。

事件类型包括 RULE_PUBLISHED、CLAIM_PUBLISHED、ORDER_RECORDED、ORDER_AMENDED、EVIDENCE_RECEIVED、CASE_OPENED、CASE_FROZEN、MEDIATION_DECIDED、REMEDY_CONFIRMED；聚合对象包括 rule_book、merchant_claim、consumer_order、evidence_item、mediation_case。

## 核心规则

- **版本化承诺**：商家承诺按版本追加，历史版本永不改写；立案时按订单时间选定适用版本，商家下架活动后仍可说明当时承诺。
- **绿色行动计入**：一次订单可含餐饮、礼盒、出行权益，只有同时被承诺版本（含适用商品与时间）和立案所绑定规则版本认可的条目才计入；退款或改签只追加变更记录，不抹掉原承诺与已计入结果。
- **幂等与冲突**：同一凭证重复上传返回既有受理结果；内容冲突保留原凭证并冻结争议，不任选一份。
- **权限边界**：订单证据与立案需消费者本人授权；商家不能修改消费者原始凭证；调解结论只能由调解员登记；调解员视图对个人信息脱敏，只呈现必要字段。
- **规则版本**：规则只增不改，更新仅适用于新立案案件，既有案件仍按立案时版本计入。
- **持续性**：受理结果与响应期限写入事件存储，平台重启后重放即恢复；向外部投诉渠道的同步是旁路的，渠道暂不可用时事件留在本地待同步，恢复后续传且不重复。
- **可解释性**：`explain_case` 说明每项主张采用的规则版本与承诺版本、经手人、结论，以及是否形成可执行的补偿或整改。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```
