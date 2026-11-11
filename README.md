# 稀有场景数据的多维配额预算选样数据包

场景候选与配额表达式资料库，具备安全集合谓词、组成本核算，以及组不可拆、场景去重的
周批次选样求解与独立复核。数据文件位于samples，字段和边界见docs/domain.md，
持久目录接口见docs/storage.md，求解器状态语义、冲突核证明与复核流程见docs/selection.md。

## 本地运行

Python 3.11以上，仅依赖标准库，无需真实账号或外部数据服务。建议以隔离模式运行，
数据路径通过参数传入，避免当前目录内的同名模块被解释器加载。

```bash
# 周批次选样：精确求解（随附规模）或带时限有界求解，落盘前自动独立校验
python -I scripts/cli_runner.py select \
  samples/candidates.json samples/quotas.json samples/constraints.json \
  -o output/selection.json --mode auto

# 从原始输入独立复核一份 selection.json
python -I scripts/cli_runner.py verify \
  samples/candidates.json samples/quotas.json samples/constraints.json \
  output/selection.json

# 既有资料检查与测试
python -I scripts/cli_runner.py inspect samples
python -I scripts/run_tests.py
python -I -m compileall -q datapack tests scripts
```

选样退出码：0最优/可行，2输入错误（区别于无解），3已证明无解，4有界但状态未知，5复核失败。
求解器区分 optimal/feasible/unknown/infeasible 四态，无解时给可重复核验的包含极小冲突核，
并点名确切受影响的配额，不把“没找到”称为无解。

## 容器

```bash
docker build -t local-datapack .
docker run --rm --network none local-datapack
docker run --rm --network none local-datapack python -I scripts/run_tests.py
```

应用没有必须常驻的服务，入口和库可在同一容器离线运行。state和output是本地工作目录，已排除出Git。样本文件只读，修订目录使用独立SQLite文件，旧版本可继续查询。

## 目录

datapack/storage.py提供摘要、原子文件与修订目录；datapack/records.py提供文件读取及重复主键分类；datapack/domain.py提供当前领域原语；datapack/selection.py提供配额预算建模、精确/有界求解、冲突核证明与独立结果校验；scripts提供隔离模式启动器；tests验证这些行为。数据加工结果不由资料检查命令自动生成。
