# 周批次选样求解器

`datapack/selection.py` 在 `domain.py` 的安全谓词（`matches`）、组成本（`group_costs`）
和去重覆盖（`coverage`）原语之上，实现组不可拆、场景去重的多维配额+预算选样。
仅依赖标准库，不调用任何远端求解服务。

## 输入与硬限制

- 候选记录字段：`scene_id`、`group_id`、`bytes`（正整数）、`value`（非负整数）、
  `city`、`tags`（字符串数组）、`eligible`（布尔）。出现任何未知字段即输入错误。
- 组不可拆：组内只要有一条 `eligible=false`，整组失去资格；必选约束指向这种组时
  得到的是**已证明无解**，不是输入错误。
- 配额：`{id, predicate, min}` 谓词仅允许 `tag/all/any/not` 嵌套（深度≤16），
  不执行任意 Python 表达式；或 `{id, city, max}` 城市独立场景上限。
- 约束：`budget`（入选组实际字节和上限）、`required`（必选组）、`exclusive`（互斥组）。
- 引用不存在的组/城市、未知 `kind`、重复 id、未知字段，一律 `InputError`
  （CLI 退出码 2），与“业务无解”严格区分。
- 重复 `scene_id`：内容完全相同则去重计一次独立场景；内容冲突则输入错误。
- 预算按入选组各记录**实际字节之和**计，不用去重后的行数估算体积。

## 优化目标（字典序）

1. 最大化入选组覆盖的**独立场景** `value` 之和（重叠场景只计一次）；
2. 并列时最小化实际字节数；
3. 仍并列时取“组编号排序后列表”字典序最小者。

合格组按编号排序后做“先纳入、后排除”的深度枚举，因此第 3 条与输入记录顺序无关，
复核人员交换两条同分方案的输入行顺序，选择保持不变。

## 状态语义（不可混用）

| 状态 | 含义 |
| --- | --- |
| `optimal` | 完整枚举+分支限界证明的全局最优，随附规模（≤22 合格组，见 `EXACT_GROUP_LIMIT`）必达 |
| `feasible` | 时限中断但持有可行解，附 `lower_value ≤ upper_value ≤ global_relaxed_upper_value` 的可核对界，不声称最优 |
| `unknown` | 时限中断且尚无可行解；**绝不**等同于无解 |
| `infeasible` | 完整枚举证明无解，附包含极小冲突核（MUS）及可重复核验的证明 |

`auto` 模式按合格组数自动选择：≤22 组走精确枚举，否则走带时限分支限界（默认 5 秒，
可用 `--time-limit` 覆盖）。“没找到”永远不会被报告成 `infeasible`。

## 无解证明与冲突核

`extract_conflict` 用删除法求包含极小 unsat core：

1. 对约束全集做完整枚举，确认整体无解（记录访问节点数与组合空间）；
2. 按固定顺序逐条尝试从当前核中删除约束，删除后若可行则该约束保留，否则移除；
3. 对终核每个成员再做一次“终核减该成员”的可行性见证。

核内每条约束带 `diagnostics`：预算/必选给实际成本对比，覆盖配额给“满足它所需的
最低组合成本”与当前预算的明确结论，因此削减预算后报告会点名**确切受影响的配额**，
而不是只打印“预算不足”。复核者重跑这些枚举即可独立复现证明。

## selection.json 与独立复核

`build_report` 写出的报告含：输入文件路径与三份 SHA-256 指纹、入选组、
每个入选场景的 `source_row_index`（可追原记录）、逐组选择理由（新增独立场景、
对各配额贡献、城市、字节）、逐约束覆盖/占用与余量、搜索统计、状态与界、冲突核。

`verify_selection` 从**原始输入**重新建模并独立复核：指纹一致、组资格、字节与价值
重算、逐约束满足、用 `domain.coverage` 交叉核对覆盖数字；对 `optimal` 且
≤20 合格组的实例，另跑一套与主 DFS 代码路径分离的 `itertools` 全组合穷举
（`_reference_optimal`）逐项比对三元目标；对 `infeasible` 重放冲突核证明；
对 `feasible/unknown` 校验界的健全性。报告被篡改时复核失败（CLI 退出码 5）。

## 命令

```bash
# 求解并写 selection.json（落盘前自动独立校验）
python -I scripts/cli_runner.py select \
  samples/candidates.json samples/quotas.json samples/constraints.json \
  -o output/selection.json --mode auto

# 对已有 selection.json 做独立复核
python -I scripts/cli_runner.py verify \
  samples/candidates.json samples/quotas.json samples/constraints.json \
  output/selection.json
```

退出码：`0` 最优/可行，`2` 输入错误，`3` 已证明无解，`4` 有界但状态未知，`5` 复核失败。
`-I` 隔离模式不读取环境变量与当前目录隐式路径，数据路径一律由参数传入。
