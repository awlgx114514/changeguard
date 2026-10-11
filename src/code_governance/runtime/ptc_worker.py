"""Private interpreter process for the PTC AST subset; it never calls ``exec``."""

import ast
import json
import sys


def _value(node, env):
    if isinstance(node, ast.Constant): return node.value
    if isinstance(node, ast.Name): return env[node.id]
    if isinstance(node, ast.List): return [_value(item, env) for item in node.elts]
    if isinstance(node, ast.Tuple): return tuple(_value(item, env) for item in node.elts)
    if isinstance(node, ast.Dict): return {_value(key, env): _value(value, env) for key, value in zip(node.keys, node.values)}
    if isinstance(node, ast.Subscript): return _value(node.value, env)[_slice(node.slice, env)]
    if isinstance(node, ast.Compare):
        left = _value(node.left, env)
        for operator, right_node in zip(node.ops, node.comparators):
            right = _value(right_node, env)
            if not _compare(operator, left, right): return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        values = [_value(item, env) for item in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len": return len(_value(node.args[0], env))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        arguments = [_value(argument, env) for argument in node.args]
        sys.stdout.write(json.dumps({"type": "tool_call", "name": node.func.attr, "args": arguments}, ensure_ascii=False) + "\n"); sys.stdout.flush()
        reply = json.loads(sys.stdin.readline())
        return reply.get("data")
    raise ValueError("unsupported restricted expression")


def _slice(node, env):
    if isinstance(node, ast.Slice): return slice(_value(node.lower, env) if node.lower else None, _value(node.upper, env) if node.upper else None, None)
    return _value(node, env)


def _compare(operator, left, right):
    return {ast.Eq: left == right, ast.NotEq: left != right, ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right, ast.GtE: left >= right}[type(operator)]


def _run(tree):
    env = {}
    def statements(items):
        for statement in items:
            if isinstance(statement, ast.Assign): env[statement.targets[0].id] = _value(statement.value, env)
            elif isinstance(statement, ast.If): statements(statement.body if _value(statement.test, env) else statement.orelse)
            elif isinstance(statement, ast.For):
                values = range(*[argument.value for argument in statement.iter.args])
                for value in values: env[statement.target.id] = value; statements(statement.body)
            else: raise ValueError("unsupported restricted statement")
    statements(tree.body)
    return env["result"]


def main():
    request = json.loads(sys.stdin.readline())
    value = _run(ast.parse(str(request["program"]), mode="exec"))
    sys.stdout.write(json.dumps({"type": "result", "result": value}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
