# Change Contract Schema v1

## 协议边界

Change Contract 描述一次代码修改的预期，不执行代码、不读取 Git 状态，也不判断修改结果。Validator、Git Context 和 CLI 不属于本 Schema。

顶层只允许以下字段：

- `version`
- `metadata`
- `intent`
- `expected`
- `constraints`
- `verification`

五个业务区块都必须存在。旧 YAML 可以省略 `version`；加载后统一得到整数 `1`。显式版本只允许 `version: 1`，未定义版本会被拒绝。

## 字段语义

### `version`

协议版本号，不是任务版本号。v1 的规范值为整数 `1`。省略时按 v1 处理，以保持现有 YAML 格式兼容。

### `metadata`

任务元数据。`id`、`project`、`created_at` 当前均为可选字符串；区块本身必填。Schema 不推断这些字段的含义，也不生成值。

### `intent`

- `goal`：修改目标，必填且不能是空字符串或全空白字符串。
- `non_goal`：明确不做的事项，可选字符串列表，缺省或显式为空时为 `[]`。

### `expected`

- `files`：预期影响的文件路径列表，可选，缺省为 `[]`。
- `modules`：预期影响的模块标识列表，可选，缺省为 `[]`。
- `change_level`：预期修改规模，必填且只能是 `L0`、`L1`、`L2` 或 `L3`。

`files` 和 `modules` 表达预期，不直接构成禁止修改清单；实际比较由后续 Validator 负责。

### `constraints`

- `forbidden`：任务明确禁止的事项字符串列表，可选，缺省为 `[]`。

Schema 只保存约束，不执行约束检查。

### `verification`

- `tests`：完成任务需要关注的测试项字符串列表，可选，缺省为 `[]`。
- `checks`：完成任务需要关注的检查项字符串列表，可选，缺省为 `[]`。

Schema 只保存验证要求，不运行命令、不判断结果。

## 空值与未知字段

所有列表字段允许显式空列表；列表中的元素必须是字符串。必填字符串字段不能缺失，`intent.goal` 还不能为空白。任何未定义字段都会被拒绝，以避免拼写错误和无声明协议扩展。
