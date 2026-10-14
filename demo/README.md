# ChangeGuard Demo

本目录展示既有执行结果的审计、W2 失败归因和历史记录回放。三个入口均读取随包保存的真实历史执行记录，输出可以与各目录中的预期文件逐字比较。

## 运行环境

使用已经安装 ChangeGuard 及其项目依赖的 Python 环境。在项目根目录安装：

```powershell
python -m pip install -e .
```

运行 Demo 仅需项目依赖。历史执行使用过的 Windows 原生测量组件、AnyIO 工作目录和模型服务均无需启动。原始报告保留执行时的路径信息，用于记录执行来源；入口使用当前 Demo 目录定位随包文件。

## Demo A：Successful Run Audit

```powershell
python -B demo/run.py success
```

固定案例来自 `KNOWN_FIX-3ce0dd94920c4f399261d6ddb34a8b85`。输出展示 campaign、slot、run、generation，决策关联的 measurement 和 lifecycle evidence，以及已经核验提交凭据的 `COMMITTED` 决策。

`RUN_STATUS` 读取数据库中保存的 verdict。`VALIDATION` 展示本次读取时的审计检查：`execution_identity` 来自既有 CLI 的身份完整性检查，`artifact_binding` 包含随包文件摘要、原始报告与数据库决策的一致性以及决策到证据的引用检查，`integrity` 包含 SQLite 完整性和证据记录完整性。历史组件验证结果全部为 `PASS`。

预期输出：[success/expected_output.txt](success/expected_output.txt)。

## Demo B：W2 Failure Attribution

```powershell
python -B demo/run.py w2_failure
```

固定案例来自现有 W2 asset 的 `W2-03e0e373d17f453c946e8ff3fb32b9c8`。先展示保存的组件验证结果：completion record 和 deadline 为 `PASS`，direct child lifecycle 为 `FAIL`。随后展示该失败的关联记录：

1. 相同执行身份下的 measurement record。
2. 原始子进程的稳定身份绑定。
3. `WaitForSingleObject(handle, 0)` 返回 `WAIT_TIMEOUT=258`，保存的生命周期事实为 `DIRECT_CHILD_LIVE_CONFIRMED`。
4. 对应 asset 注册的 `cleanup-resource-release` 缺陷。
5. 保存的 `confirmed_rejection` 归因和 terminal decision。
6. 已核验的提交记录，以及既有 replay 分析得到的 `Lifecycle Observation`。

measurement、child binding、lifecycle 和 terminal identity 均直接列出，便于在原始记录中查找。W2 asset 和 mutation 保持原状。

预期输出：[w2_failure/expected_output.txt](w2_failure/expected_output.txt)。

## Demo C：Audit Replay

```powershell
python -B demo/run.py audit_replay
```

输出 JSON 将 `STORED_FACTS` 与 `DERIVED_ANALYSIS` 分开，包含原始 `TASK_VERDICT=FAIL`、派生的 `EARLIEST_OBSERVED_FAILURE=Lifecycle Observation` 及其数据库记录引用，随后列出 source binding 和 ordering 的覆盖状态。

Audit Replay 不启动 Agent、不调用执行工具、不重新计算原始 verdict。入口通过现有 CLI 的 `audit` 读取保存的决策，通过 `replay` 分析已捕获记录之间的关系。它不调用 Task3 scoring consumer。

验收测试在真实入口运行期间记录 Python 函数调用、模块导入和系统审计事件，检查 Agent、runtime、tools、verification、validator 及 Task3 模块均未执行，检查进程启动、网络操作和文件写入均未发生，并核对运行前后所有 Demo 文件的摘要和文件列表。

预期输出：[audit_replay/expected_output.json](audit_replay/expected_output.json)。

## Shell 入口

在支持 POSIX shell 的环境中，可以使用三个对应的 `run.sh`。`PYTHON` 指定已安装本项目依赖的解释器，默认使用 `python`。

```sh
sh demo/success/run.sh
sh demo/w2_failure/run.sh
sh demo/audit_replay/run.sh
```

Windows 可以直接使用前面的 Python 命令；验收测试另外使用 Git Bash 执行三个 Shell 入口。

## 原始审计输出

需要查看完整记录时，使用现有 CLI：

```powershell
changeguard audit W2-03e0e373d17f453c946e8ff3fb32b9c8 --repo demo --source demo/data/w2/campaign.sqlite3 --format json
changeguard replay W2-03e0e373d17f453c946e8ff3fb32b9c8 --repo demo --source demo/data/w2/campaign.sqlite3 --format json
```

Demo adapter 调用相同的 CLI `main`，负责筛选展示字段和检查示例所需记录是否齐全。随机 capture identity、读取时间、个人路径和文件系统编号保留在完整 CLI 输出中；稳定的 Demo 输出展示原生执行身份和与证据有关的字段。

## 随包记录与覆盖范围

`data/success/` 和 `data/w2/` 各包含一份原始 `campaign.sqlite3` 与原始执行报告 `record.json`。文件来自既有 `final-evidence-v2` 执行结果，保留原始字节。`data/manifest.json` 记录运行身份、asset profile、benchmark 实现提交及各文件的 SHA256。每次运行首先核对摘要；文件缺失、摘要不符、证据引用缺失或提交记录缺失都会立即报错。

`EVIDENCE_STATUS=VALID` 及 `EVIDENCE` 中的 `VALID` 表示所选记录的完整性和关联引用通过检查。`artifact_binding` 展示决策与随包证据的关联。独立的 `COVERAGE` 保留现有审计的评估：

- `audit_status=PARTIAL`：现有资料的历史覆盖有限。
- `source_binding=UNVERIFIABLE`、`source_binding_reason=NOT_PROVEN_WITHIN_CAPTURE`：捕获范围内没有独立证明源码与执行环境之间的绑定。
- `producer_authority=UNVERIFIABLE`、`support=LIMITED_BY_PRODUCER_TRUST`：原始生产者的权威性受既有审计边界限制。
- `discovery=DISCOVERY_UNKNOWN`：全部历史资料的发现完整性未知。
- `ordering=PARTIAL_ORDER`、`boundary_selection=PARTIAL_HISTORY`：分析只使用已有顺序关系，失败边界限于已观测历史。

Demo 成功展示并通过检查时返回退出码 `0`。上述原始 CLI 命令保留现有 `PARTIAL` 对应的退出码 `1`。历史任务的 `FAIL` 作为保存的事实展示。

三个 Demo 展示已经完成的执行，不重新执行历史任务。数据库中的 Windows 观测值按历史事实读取，当前展示过程不重新查询历史子进程。

## 验收测试

```powershell
python -B -m pytest tests/demo tests/audit -q --basetemp=.tmp/demo-acceptance
```

测试覆盖三个真实入口及预期输出、完整目录搬移、包含空格的目录、Shell 入口、只读行为、损坏和缺失的随包文件，以及文件摘要匹配时仍然存在的证据引用缺失和提交记录缺失。

本目录及其验收测试为本轮修改范围。核心架构、production runtime、Task3 contract、scoring 语义、benchmark 能力、治理机制以及项目根目录 README 保持原状。
