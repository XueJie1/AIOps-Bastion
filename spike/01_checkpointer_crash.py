"""
Spike 01 — LangGraph interrupt + Checkpointer 崩溃恢复 (验证 §11.1)

假设: interrupt() 挂起态经 SqliteSaver 持久化;进程崩溃重启后,
      从检查点恢复时只读工具不重放, L3 不重复执行。

做法: 硬编码图(无 LLM, 排除非确定性):
      plan -> readonly_tool(副作用计数写文件) -> l3_node(interrupt) -> exec(计数)
      跑到 interrupt -> 关闭 conn 模拟崩溃 -> 新进程同 thread_id + Command(resume)
      -> 检查计数 + 状态变量恢复

PASS: readonly 计数=1(未重放); L3 exec=1(执行一次); 状态 resumed=True
FAIL:  记录到 REPORT.md
"""
import json
import sys
import sqlite3
import tempfile
from pathlib import Path
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver


# ---------- 状态 ----------
class State(TypedDict):
    readonly_calls: int
    l3_exec_calls: int
    last_node: str
    resumed: bool


# ---------- 副作用计数文件(跨进程, 模拟"真实副作用") ----------
COUNTER_FILE = Path(tempfile.gettempdir()) / "spike01_counter.json"


def _read_counter() -> dict:
    if COUNTER_FILE.exists():
        return json.loads(COUNTER_FILE.read_text())
    return {"readonly_calls": 0, "l3_exec_calls": 0}


def _write_counter(d: dict) -> None:
    COUNTER_FILE.write_text(json.dumps(d))


def reset_counter() -> None:
    _write_counter({"readonly_calls": 0, "l3_exec_calls": 0})


# ---------- 节点 ----------
def plan_node(state: State) -> dict:
    return {"last_node": "plan"}


def readonly_node(state: State) -> dict:
    """模拟只读探测工具调用(有副作用计数, 用来检测是否被重放)。"""
    c = _read_counter()
    c["readonly_calls"] += 1
    _write_counter(c)
    print(f"  [readonly_node] 调用 #{c['readonly_calls']} (真实副作用)")
    return {"readonly_calls": c["readonly_calls"], "last_node": "readonly"}


def l3_node(state: State) -> dict:
    """模拟 L3 工具: 先 interrupt 等审批, resume 后才 exec。"""
    print("  [l3_node] 触发 interrupt (写 hitl_requests PENDING)")
    approval = interrupt({"target_host": "node-a", "action_type": "restart_service"})
    print(f"  [l3_node] resume 收到 approval: {approval}")
    c = _read_counter()
    c["l3_exec_calls"] += 1
    _write_counter(c)
    print(f"  [l3_node] 执行修复 (L3 exec #{c['l3_exec_calls']})")
    return {
        "l3_exec_calls": c["l3_exec_calls"],
        "last_node": "l3_exec",
        "resumed": True,
    }


def done_node(state: State) -> dict:
    return {"last_node": "done"}


def build_graph(checkpointer):
    g = StateGraph(State)
    g.add_node("plan", plan_node)
    g.add_node("readonly", readonly_node)
    g.add_node("l3", l3_node)
    g.add_node("done", done_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "readonly")
    g.add_edge("readonly", "l3")
    g.add_edge("l3", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=checkpointer)


CHECKPOINTER_DB = Path(tempfile.gettempdir()) / "spike01_checkpoints.sqlite"
THREAD_ID = "spike-01-thread"


def run_phase(phase: str, app, *, inject_resume: bool):
    print(f"\n=== {phase} ===  (inject_resume={inject_resume})")
    config = {"configurable": {"thread_id": THREAD_ID}}

    if inject_resume:
        result = app.invoke(
            Command(resume={"approval_id": "approval-test-001"}),
            config=config,
        )
        print(f"  resume 后状态: {result}")
        return result
    else:
        result = app.invoke({"readonly_calls": 0, "l3_exec_calls": 0}, config=config)
        print(f"  invoke 返回(应含 interrupt): {result}")
        snap = app.get_state(config)
        print(f"  当前状态快照: next={snap.next} values={snap.values}")
        return snap.values


def main():
    reset_counter()
    if CHECKPOINTER_DB.exists():
        CHECKPOINTER_DB.unlink()

    print("=" * 60)
    print("PHASE 1: 首次执行, 应在 l3_node interrupt 处挂起")
    print("=" * 60)
    conn = sqlite3.connect(str(CHECKPOINTER_DB), check_same_thread=False)
    saver = SqliteSaver(conn)
    app = build_graph(saver)
    run_phase("PHASE 1", app, inject_resume=False)

    # 模拟崩溃: 丢弃 app + 关闭 conn
    print("\n--- 模拟崩溃: 丢弃 app + saver, 新建进程 ---")
    del app
    conn.close()

    print("\n" + "=" * 60)
    print("PHASE 2: 新进程恢复, 同 thread_id, resume")
    print("=" * 60)
    conn2 = sqlite3.connect(str(CHECKPOINTER_DB), check_same_thread=False)
    saver2 = SqliteSaver(conn2)
    app2 = build_graph(saver2)
    state_after_phase2 = run_phase("PHASE 2", app2, inject_resume=True)
    conn2.close()

    # ---------- 断言 ----------
    print("\n" + "=" * 60)
    print("PHASE 3: 验证结果")
    print("=" * 60)
    final_counter = _read_counter()
    print(f"最终计数器: {final_counter}")
    print(f"PHASE 2 后状态: {state_after_phase2}")

    failures = []
    if final_counter["readonly_calls"] != 1:
        failures.append(
            f"FAIL: readonly_calls 期望 1(未重放), 实际 {final_counter['readonly_calls']}"
        )
    if final_counter["l3_exec_calls"] != 1:
        failures.append(
            f"FAIL: l3_exec_calls 期望 1(执行一次), 实际 {final_counter['l3_exec_calls']}"
        )
    if not state_after_phase2.get("resumed"):
        failures.append("FAIL: 状态 resumed 未恢复为 True")

    print("\n" + "-" * 40)
    if failures:
        for f in failures:
            print(f)
        print("\n>>> 01 结论: FAIL")
        sys.exit(1)
    else:
        print("readonly_calls=1 (只读未重放) ✓")
        print("l3_exec_calls=1 (L3 执行一次) ✓")
        print("状态 resumed=True ✓")
        print("\n>>> 01 结论: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
