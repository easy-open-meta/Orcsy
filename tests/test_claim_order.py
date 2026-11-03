"""任务领取顺序自检：手动 order（看板拖拽序）优先，未排序回退优先级/id。

风格与 test_project_llm.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.task_model import parse_task_file, scan_tasks, set_task_file_order


def make_task(root: Path, tid: str, priority: str, order: int | None = None) -> None:
    fm = f"---\ntitle: {tid}\npriority: {priority}\nstatus: open\n"
    if order is not None:
        fm += f"order: {order}\n"
    fm += "---\n\n正文。\n"
    (root / f"{tid}.md").write_text(fm, encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="order-"))
    try:
        # 混合场景：优先级始终优先（P0 排最前），同 P 内按 order；无 order 的
        # 排在同优先级已排序任务之后，与其他同 P 任务按 id
        make_task(tmp, "A-p2-ord2", "P2", order=2)
        make_task(tmp, "B-p0-ord1", "P0", order=1)
        make_task(tmp, "C-p1-nord", "P1")
        make_task(tmp, "D-p2-ord1", "P2", order=1)   # 同 P2 内 order 更小 → 先于 A
        make_task(tmp, "E-p4-nord", "P4")
        make_task(tmp, "F-p2-nord", "P2")            # 无 order → 排在 A/D 之后
        tasks, errors = scan_tasks(tmp)
        assert not errors
        got = [t.id for t in tasks]
        assert got == ["B-p0-ord1", "C-p1-nord", "D-p2-ord1", "A-p2-ord2",
                       "F-p2-nord", "E-p4-nord"], got
        print("ok: 优先级优先，同 P 内按 order，无 order 排队尾")

        # 纯无 order 场景：与旧排序完全一致（回归）
        for f in tmp.glob("*.md"):
            f.unlink()
        make_task(tmp, "X-p1", "P1")
        make_task(tmp, "A-p1", "P1")
        make_task(tmp, "M-p0", "P0")
        tasks, _ = scan_tasks(tmp)
        assert [t.id for t in tasks] == ["M-p0", "A-p1", "X-p1"]
        print("ok: 无 order 时排序与旧行为一致（P→id）")

        # set_task_file_order roundtrip：写入 → 重扫排序生效 → 清除回退
        for f in tmp.glob("*.md"):
            f.unlink()
        make_task(tmp, "T1", "P2")
        make_task(tmp, "T2", "P2")
        set_task_file_order(tmp / "T2.md", 1)
        t = parse_task_file(tmp / "T2.md")
        assert t.order == 1
        assert [x.id for x in scan_tasks(tmp)[0]] == ["T2", "T1"]
        set_task_file_order(tmp / "T2.md", None)
        assert parse_task_file(tmp / "T2.md").order is None
        assert [x.id for x in scan_tasks(tmp)[0]] == ["T1", "T2"]  # 回退 P 相同按 id
        print("ok: set_task_file_order 写入/清除 → 领取顺序联动")

        # ---- 受阻任务重新入队插队（requeue_seq）----
        from orcsy.task_model import set_task_file_status
        for f in tmp.glob("*.md"):
            f.unlink()
        make_task(tmp, "OLD-p1", "P1")
        make_task(tmp, "BLK-p1", "P1")
        set_task_file_status(tmp / "BLK-p1.md", "blocked")   # Agent 标受阻（清除戳）
        assert parse_task_file(tmp / "BLK-p1.md").requeue_seq == 0
        set_task_file_status(tmp / "BLK-p1.md", "open")      # 重新入队（写递增戳）
        seq1 = parse_task_file(tmp / "BLK-p1.md").requeue_seq
        assert seq1 == 1, seq1
        make_task(tmp, "NEW-p0", "P0")                        # 新的高优任务
        tasks, _ = scan_tasks(tmp)
        assert [t.id for t in tasks] == ["BLK-p1", "NEW-p0", "OLD-p1"], \
            [t.id for t in tasks]  # 重入队任务插到 P0 新任务之前
        print("ok: 重新入队的受阻任务插队（越过新 P0 任务优先领取）")

        # 多个重入队任务：越近受阻的越先（后端失败后前端跟着失败的场景）
        set_task_file_status(tmp / "OLD-p1.md", "blocked")
        set_task_file_status(tmp / "OLD-p1.md", "open")       # seq=2，比 BLK-p1 更近
        tasks, _ = scan_tasks(tmp)
        assert [t.id for t in tasks][:2] == ["OLD-p1", "BLK-p1"], [t.id for t in tasks]
        print("ok: 多个重入队任务按受阻新近度排序")

        # 领取后开工清戳（agent._mark_in_progress 行为）：不再占用插队位
        from orcsy.task_model import _set_frontmatter_field
        _set_frontmatter_field(tmp / "OLD-p1.md", None, "requeue_seq", None)
        assert parse_task_file(tmp / "OLD-p1.md").requeue_seq == 0
        tasks, _ = scan_tasks(tmp)
        # OLD-p1 清戳后回到常规排序；BLK-p1 仍带 seq=1 保持插队
        assert [t.id for t in tasks] == ["BLK-p1", "NEW-p0", "OLD-p1"], \
            [t.id for t in tasks]
        print("ok: 开工后清除受阻戳，插队位释放（其余重入队任务不受影响）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n任务领取顺序自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
