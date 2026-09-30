# -*- coding: utf-8 -*-
"""chronicles —— 面向 agent 的统一命令面（技能包落地 · 第 0 步）。

**本模块是纯包装**，只做四件事：
    ① 解析参数 → ② 转发给**既有 CLI**（唯一实现，不另造）→
    ③ 读回**已经落盘**的产物组装 JSON → ④ 把结果映射成有区分度的退出码。

它**不复制任何业务逻辑**（几何、判据、校验一律复用既有单一实现）。
底层 CLI 的行为、产物、路径口径一个字都不改。

设计依据：命令面设计 §4 与 §7 第 0 步。

为什么不是给每个模块各写一份 CLI
    既有 5 个 CLI 各自已经零交互、返回 int（§4.1 已核实）。agent 缺的不是"能跑"，
    而是 ① 一个入口 ② 结构化输出 ③ **能自纠的退出码**。三者都是接口层的活。

退出码契约（agent 靠这个自纠，不靠读自然语言）
    0   成功
    2   面外拒答（**正确行为**，不是错误）—— 由 `triage` 产生（第 1 步已落地）
    3   缺料（待补输入，agent 可自纠：去补文件）
    4   校验不过（料齐了，但结果不合格）
    1   内部错误（不该发生，需人看）
    64  用法错误（参数写错）

    ★ 3 与 2 必须分开（§4.3）；★ 64 与 2 **也必须分开** —— 否则 agent 会把
    「参数打错」误判成「材料面外」，而这两者的下一步动作完全相反。
    argparse 的默认用法错误码恰好是 2，会撞车，故本模块把它改成 64。

输出约定（§4.2）
    `--json` 给出：stdout **只有** JSON（可被 agent 直接 parse）；
                   底层的人读输出改道 **stderr**。
    未给：底层输出原样走 stdout（保持既有 CLI 的人工体验）。

用法
    python chronicles.py triage --profile prof_xxx --inbox inbox/
    python chronicles.py triage --profile prof_xxx --project <栏目id> --json
    python chronicles.py facts  --profile prof_xxx
    python chronicles.py plan   --profile prof_xxx
    python chronicles.py exec   --profile prof_xxx
    python chronicles.py verify --result data/results/xxx.result.json --json
    python chronicles.py map    --profile prof_xxx --svg out.svg --json
    python chronicles.py ocr    --images inbox/ --json
    python chronicles.py import --images "<材料目录>" --json
    python chronicles.py draft  --profile prof_xxx --json      # 产物 → 裁决草稿（人面入口）
    python chronicles.py annotate lines --image 0001.png --json
    python chronicles.py annotate add   --image 0001.png --line 3 --attr 公司名
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "0.1.0"

# ---- 退出码契约：唯一正本在 `cli_contract`（2026-09-24 收敛，此前抄 8~10 份）----
# ★ 再导出保持 `chronicles.EXIT_*` / `EXIT_MEANING` 的既有引用面不变
#   （tests 与 MCP server 的 `_exit_legend` 都从这里读）。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_OUT_OF_SCOPE,       # noqa: E402
                          EXIT_MISSING, EXIT_INVALID, EXIT_USAGE,          # noqa: E402
                          EXIT_MEANING)                                    # noqa: E402

# ---- 路径口径：与既有模块**逐字一致**，不另造（§60）----
#   ⚠ facts 在 data/plan/（单数），plan 在 data/plans/（复数）—— 别写反。
PROFILES_DIR = ROOT / "data" / "collection_profiles"
FACTS_DIR = ROOT / "data" / "plan"
PLANS_DIR = ROOT / "data" / "plans"
RESULTS_DIR = ROOT / "data" / "results"
STRUCTURED_DIR = ROOT / "data" / "structured"


# ============================================================
# 通用小工具
# ============================================================
def _read_json(p: Path) -> Optional[Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _emit(as_json: bool, payload: Dict[str, Any]) -> None:
    """结构化结果 → stdout（仅 json 模式；人读模式不额外说话）。"""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _mirror(fmt: Callable[[Any], str], r: Any, as_json: bool) -> None:
    """人读视图 —— **只负责显示，绝不参与判定**（技能包第 4 步实测缺陷的根因修复）。

    ★ 为什么必须收口：`cmd_*` 里「算 rc → 打人读视图 → `return rc`」在**同一条进程
      路径**上。只要 `format_human` 抛异常，`sys.exit(rc)` 就变成 **1（内部错误）**
      —— 一个**显示层**的 bug 把「正确的成功」报成「系统故障」，
      agent 会按「1 = 需人看」停手。**这是契约倒置，不是排版问题。**

      2026-09-15 实测：`project images`（`result['project']` 是 str 而格式化器当 dict 用）
      ⇒ `AttributeError` ⇒ rc **0 → 1**；`--json` 路径同样中招
      （它打完 JSON 还要往 stderr 打人读镜像）。

    ⇒ 显示失败只降级为一行提示，**退出码仍由 `result['exit_code']` 决定**。
    """
    try:
        text = fmt(r)
    except Exception as e:  # noqa: BLE001 —— 显示层不许影响判定
        text = (f"⚠ 人读视图生成失败（{type(e).__name__}: {e}）"
                "—— 机器视图仍有效，退出码不受影响。")
    print(text, file=sys.stderr if as_json else sys.stdout)

def _fail(code: int, what: str, next_step: str, as_json: bool,
          extra: Optional[Dict[str, Any]] = None) -> int:
    """报错要给「哪里不对」+「下一步怎么办」（§4.2 报错可执行）。

    `extra`：把底层结果里的**机器可判别字段**（如批量安全门的 `blocked` /
    `batch_block_reason`）一并带进 JSON —— 只报 `error` 一句人话，agent 就得
    回去读源码才能分辨"哪一道门"。human 侧不受影响。
    """
    line = f"[chronicles] {EXIT_MEANING.get(code, code)}：{what}"
    if as_json:
        payload: Dict[str, Any] = {"ok": False, "exit": code, "error": what,
                                   "exit_meaning": EXIT_MEANING.get(code),
                                   "next": next_step}
        if extra:
            payload.update(extra)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"{line}\n  → {next_step}", file=sys.stderr)
    else:
        print(line)
        print(f"  → {next_step}")
    return code


def _run_entry(entry: Callable[[Sequence[str]], int], argv: List[str],
               as_json: bool) -> int:
    """调底层 CLI。json 模式下把它的 stdout 改道 stderr，保住 stdout 的纯净。"""
    ctx = redirect_stdout(sys.stderr) if as_json else nullcontext()
    with ctx:
        return entry(argv)


# ============================================================
# 子命令
# ============================================================
def cmd_triage(a: argparse.Namespace) -> int:
    """分诊：这批页像不像**这个档案**（第 1 步新增 —— 退出码 2 的第一个产生者）。

    ⚠ 与其余子命令不同，本命令**不落任何产物**（分诊零 token、零产物、只读），
      所以它不是「转发 CLI + 读回产物」，而是**直接取用** `page_triage.run_triage`
      —— 它同时产出结果与 `exit_code`（唯一判定点），本函数**不重判**。
      这样同一批页不可能出现两个退出码（§60 单一实现纪律）。
    """
    import page_triage as PT

    t = PT.run_triage(profile=a.profile, inbox=a.inbox, project_id=a.project_id,
                      structured_dir=a.structured_dir, outbox_dir=a.outbox,
                      allow_suspect=a.allow_suspect)
    rc = int(t.get("exit_code", EXIT_INTERNAL))

    if not a.as_json:
        _mirror(PT.format_human, t, as_json=False)
        return rc

    payload = {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "triage", "schema_version": SCHEMA_VERSION,
        "profile": a.profile, "source": t.get("source"),
        "verdict": t.get("verdict"),
        "n_pages": t.get("n_pages"), "n_ok": t.get("n_ok"),
        "n_suspect": t.get("n_suspect"), "n_unmeasurable": t.get("n_unmeasurable"),
        "anchors_available": t.get("anchors_available"),
        "thresholds": t.get("thresholds"),
        "allow_suspect": t.get("allow_suspect"),
        "suspects": [p.get("stem") for p in (t.get("suspects") or [])],
        "unmeasurable": [p.get("stem") for p in (t.get("unmeasurable") or [])],
        "pages": t.get("pages"),
        "elapsed_ms": t.get("elapsed_ms"),
        "advice": t.get("advice") or t.get("error"),
        "next": PT.advice_for(t),
        "read_discipline": t.get("_read"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _mirror(PT.format_human, t, as_json=True)
    return rc


def cmd_facts(a: argparse.Namespace) -> int:
    """层 1：金标准 → facts（只记录观测，不推断规则）。"""
    pid = a.profile
    prof_p = PROFILES_DIR / f"{pid}.json"
    if not prof_p.exists():
        return _fail(EXIT_MISSING, f"档案不存在：{prof_p}",
                     "核对 profile id（见 data/collection_profiles/），"
                     "或先在界面里建/导入档案。", a.as_json)

    import gold_facts as GF

    argv = ["--profile", pid] + (["--out", a.out] if a.out else [])
    # ★ 2026-09-16：`--gold` 与 `profile learn` 的**同名同义**（同一件事两个名字
    #   迟早漂）。注意它只换**目录**；"目录里哪些行算数"由 `--profile` 决定 ——
    #   隔离在 `gold_facts.build_facts` 里，不靠调用方记得传对目录。
    if getattr(a, "gold", None):
        argv += ["--gold", a.gold]
    rc = _run_entry(GF._main, argv, a.as_json)

    out_dir = Path(a.out) if a.out else FACTS_DIR
    facts_p = out_dir / f"facts_{pid}.json"
    facts = _read_json(facts_p)
    rc = GF.facts_exit_code(rc, facts)          # ★ 判定点在数据层，包装层不重判
    _emit(a.as_json, {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "facts", "schema_version": SCHEMA_VERSION,
        "profile": pid, "artifact": str(facts_p),
        "facts": facts,
    })
    return rc


def cmd_gold(a: argparse.Namespace) -> int:
    """金标准普查：「我有哪些档案 / 哪些项目、金标准做到哪一步」（只读、零 token）。

    ★ 为什么它是**隔离的配套**（2026-09-16）：隔离一旦生效，「库里 7 页只用了 4 页」
      就成了一件必须**看得见**的事 —— 否则人只会以为数据丢了，而不是隔离起了作用。
      本命令把「本档案 N 页 ｜ 排除他档案 M 行 ｜ 排除未归档 K 行」直接印在人面上。

    ★ 与 `/api/seam_subjects` 的分工（不是重复实现）：
      · 那边是**选择口**（让人挑用哪套例题，刻意列全库）；
      · 这边是**普查**（各档案各有多少、项目做到哪一步）。
      两边的数字都出自 `rule_learn.gold_census` 与 `data_io.stem_to_project`。
    """
    import gold_report as GR

    # ★ 失败通道（2026-09-24 补）：run_gold 是纯只读聚合，但数据目录不可读等
    #   异常此前会裸穿透（无信封、无 next，agent 无从自纠）。接住 ⇒ 1 + 信封。
    try:
        r = GR.run_gold(gold_dir=a.gold)
    except Exception as e:                                   # noqa: BLE001
        return _fail(EXIT_INTERNAL, f"金标准普查失败：{type(e).__name__}: {e}",
                     "多为数据目录 / 规则文件不可读 —— 看 stderr 栈定位；"
                     "本命令只读，修复后重跑即可。", a.as_json)
    rc = int(r.get("exit", EXIT_OK))
    if not a.as_json:
        _mirror(GR.format_human, r, as_json=False)
        return rc
    payload = dict(r)
    payload.update({"schema_version": SCHEMA_VERSION, "exit": rc,
                    "next_command": GR.next_hint(r),
                    "exit_meaning": EXIT_MEANING.get(rc)})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


def cmd_plan(a: argparse.Namespace) -> int:
    """层 2：facts（+契约+规则）→ 识别与标注方案。这是核心一层。"""
    pid = a.profile
    prof_p = PROFILES_DIR / f"{pid}.json"
    if not prof_p.exists():
        return _fail(EXIT_MISSING, f"档案不存在：{prof_p}",
                     "核对 profile id（见 data/collection_profiles/）。", a.as_json)

    facts_dir = Path(a.facts_dir) if a.facts_dir else FACTS_DIR
    facts_p = facts_dir / f"facts_{pid}.json"
    if not facts_p.exists():
        return _fail(EXIT_MISSING, f"缺 facts：{facts_p}",
                     f"先跑 `chronicles facts --profile {pid}`。", a.as_json)

    import plan_build as PB

    argv = ["--profile", pid]
    if a.out:
        argv += ["--out", a.out]
    if a.facts_dir:
        argv += ["--facts-dir", a.facts_dir]
    if a.contracts_dir:
        argv += ["--contracts-dir", a.contracts_dir]
    if a.rules_dir:
        argv += ["--rules-dir", a.rules_dir]
    rc = _run_entry(PB.main, argv, a.as_json)

    out_dir = Path(a.out) if a.out else PLANS_DIR
    plan_p = out_dir / f"plan_{pid}.json"
    md_p = out_dir / f"plan_{pid}.md"
    plan = _read_json(plan_p)
    rc = PB.plan_exit_code(rc, plan)            # ★ 判定点在数据层，包装层不重判
    _emit(a.as_json, {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "plan", "schema_version": SCHEMA_VERSION,
        "profile": pid, "artifact": str(plan_p),
        "handbook": str(md_p) if md_p.exists() else None,
        "plan": plan,
    })
    return rc


def cmd_exec(a: argparse.Namespace) -> int:
    """层 3：plan → 产物（result.json）。

    ★ WP-3（2026-09-15）：页集合不再只有「单页 / 方案编译页」两个极值 ——
      `--pages` / `--project` 让"这批新页"成为**一等参数**，且**跑的是谁**由
      `page_source` 在**产物 / 信封 / 人读输出**三处一致声明。
      此前不给 `--stem` 就跑 `plan.source.pages`（= 金标准页），rc 0、
      有产物、校验通过 —— 跑错对象而不自知。
    """
    pid = a.profile
    plan_dir = Path(a.plan_dir) if a.plan_dir else PLANS_DIR
    plan_p = plan_dir / f"plan_{pid}.json"
    if not plan_p.exists():
        return _fail(EXIT_MISSING, f"方案不存在：{plan_p}",
                     f"先跑 `chronicles plan --profile {pid}`。", a.as_json)

    import plan_exec as PE

    plan = _read_json(plan_p) or {}
    # ★ 与底层**调同一个函数**（不是另写一份实现）：页集合的解析只有一处，
    #   所以命令面报的 `page_source` 与产物里写的一定一致。
    stems, page_source = PE.resolve_stems(plan, stem=a.stem, pages=a.pages,
                                          project=a.project_id)
    if not stems:
        return _fail(
            EXIT_MISSING, f"页集合为空（page_source={page_source}）",
            ("栏目里还没有页 —— 先 `chronicles project assign --project "
             f"{a.project_id} --images <页目录>`；或用 `--pages <目录>`。"
             if page_source.startswith("project:") else
             "给 `--pages <目录|页名…>` / `--project <栏目id>` / `--stem <页名>`。"),
            a.as_json)

    argv = ["--profile", pid]
    if a.stem:
        argv += ["--stem", a.stem]
    if a.pages:
        argv += ["--pages"] + list(a.pages)
    if a.project_id:
        argv += ["--project", a.project_id]
    if a.plan_dir:
        argv += ["--plan-dir", a.plan_dir]
    if a.out:
        argv += ["--out", a.out]
    if a.outbox:
        argv += ["--outbox", a.outbox]
    if a.structured_dir:
        argv += ["--structured-dir", a.structured_dir]
    if a.no_verify:
        argv += ["--no-verify"]
    rc = _run_entry(PE.main, argv, a.as_json)

    # 读回**已落盘**的产物（不重算；校验详情交给 `verify` 子命令，各司其职）
    out_dir = Path(a.out) if a.out else RESULTS_DIR
    sdir = Path(a.structured_dir) if a.structured_dir else STRUCTURED_DIR
    items: List[Dict[str, Any]] = []
    for s in stems:
        p = out_dir / f"{s}.result.json"
        r = _read_json(p)
        if r is None:
            # ★★ WP-3 收尾：**没产物 ≠ 没跑**。最常见的两种原因是
            #   「这页还没有 OCR 正文」与「跑了但失败」，而两者下一步动作不同
            #   （去补 OCR / 去看执行器报错）。此前两者都只是一句"跳过（无行）"
            #   打在 stderr 上，信封里什么都不剩 ⇒ agent 会把 15 页当成 15 页全成了。
            #   与"跑错对象"是同一类病：**对象级的事实必须显式**。
            items.append({
                "stem": s, "artifact": None, "written": False,
                "reason": ("no_structured" if not (sdir / f"{s}.json").exists()
                           else "no_lines_or_failed"),
            })
            continue
        recs = r.get("records") or []
        items.append({
            "stem": s, "artifact": str(p), "written": True,
            "tier": r.get("tier"),
            "n_records": len(recs),
            "n_values": sum(len(x.get("attrs") or []) for x in recs),
            "n_unassigned": len(r.get("unassigned_text") or []),
            # ★ WP-2a/2b：页图来源与页尺寸**随产物一起进信封** ——
            #   "图取到没有"（`source`）与"页多大"是判断几何是否可信的两个显式读数。
            "page_source": r.get("page_source"), "page": r.get("page"),
        })
    n_skipped = sum(1 for x in items if not x.get("written"))

    # ★ P11（2026-09-29）口 B：材料画像 QA flags 进 exec 信封 —— **呈现层 only**。
    #   QA 是推断口，只进「跑完之后」的逐页报告，不进执行顺序（R3 取证：每页
    #   独立执行独立校验，动序无收益且不可对账）。pid 从 plan 文件名现成可得。
    #   无 corpus ⇒ 键缺席，信封与现状**逐字节不变**（零影响护栏）；
    #   页在 corpus 无登记 / flags 为空 ⇒ 该页同样不给键。
    qa_by_page = _corpus_qa_by_page(pid)
    if qa_by_page:
        for x in items:
            q = qa_by_page.get(x["stem"])
            if q:
                x["qa_flags"] = q["flags"]

    ok = rc == 0
    # ★★ 2026-09-16 人面入口：**产物落盘 ≠ 有人能验收**。
    #   此前 exec 跑完就结束了，信封里没有任何"下一步去哪儿"的指认 ——
    #   用户实测的原话是「我看不到你自动标注以后的产物在哪，就更不知道成果是对的
    #   还是错的」。产物躺在 data/results/ 而**没有任何消费方**，这正是飞轮断链处。
    #   ⇒ 这里把「人面入口」写成显式读数（URL + 下一步命令），只指路、不起服务。
    written_stems = [x["stem"] for x in items if x.get("written")]
    entry = {}
    try:
        import draft_bridge as DB
        urls = DB.annotate_urls(written_stems, outbox_dir=a.outbox)
        entry = {"base_url": DB.base_url(), "urls": urls, "n_urls": len(written_stems)}
    except Exception as e:  # noqa: BLE001 —— 指路信息是增益，坏了不改判定
        entry = {"error": f"{type(e).__name__}: {e}"}
    next_step = (
        (f"`chronicles draft --profile {pid}` 把产物变成裁决草稿 → 再进界面逐条裁决"
         f"（{entry.get('base_url', '')}/annotate/<页图名>）；采纳即落 manual_annotations/。")
        if written_stems else
        "没有产物可桥 —— 见 skipped 的原因（多为该页还没有 OCR 正文：先跑 `chronicles ocr`）。"
    ) if ok else "有页不合规 —— 逐页看 verify 的报错；这与「有没有产物」是两件事。"

    _emit(a.as_json, {
        "ok": ok, "exit": EXIT_OK if ok else EXIT_INVALID,
        "subcommand": "exec", "schema_version": SCHEMA_VERSION,
        "profile": pid, "plan": str(plan_p), "results_dir": str(out_dir),
        "page_source": page_source, "n_pages": len(stems), "n_written": len(items) - n_skipped,
        # ★ "跑进去了但没出产物"必须是显式读数（此前只能从 n_pages≠n_written 猜）
        "n_skipped": n_skipped,
        "skipped": [{"stem": x["stem"], "reason": x["reason"]}
                    for x in items if not x.get("written")],
        "verified": not a.no_verify, "results": items,
        "annotate": entry, "next": next_step,
    })
    _hint = (f"[chronicles] 人面入口：{entry.get('base_url', '')}/annotate/<页图名>"
             f"（本批 {entry.get('n_urls', 0)} 页）\n[chronicles] 下一步：{next_step}")
    try:
        print(_hint, file=sys.stderr if a.as_json else sys.stdout)
    except Exception:  # noqa: BLE001 —— 显示层不许影响判定（同 `_mirror`）
        pass
    # ★ 底层语义由 `plan_exec` 单一判定，包装层只透传、不压档（2026-09-24 修）：
    #   3 = 页集合为空（**去补料**）；非 0 其余 = 有页不合规或执行失败（校验不过）。
    #   旧行为把 3 一并压成 4 —— 两处各解析一次页集合，口径漂移时会把「缺料」
    #   误报成「校验不过」，而两者的下一步动作相反。
    return EXIT_OK if ok else (EXIT_MISSING if rc == EXIT_MISSING else EXIT_INVALID)


def cmd_draft(a: argparse.Namespace) -> int:
    """产物 → 裁决草稿（**技能包「人面」的桥**，2026-09-16 用户实测缺口）。

    ★ 为什么必须有这一条：`exec` 的产物 `data/results/<stem>.result.json`
      **此前没有任何消费方** —— 裁决面（`/annotate` + `/api/preannotations/*`）
      读的是 `data/preannotations/<stem>.ai.jsonl`，而那目录只有"在界面里点生成"
      才写。于是「看一眼 agent 标得对不对 → 改一条 → 变成新一轮金标准页」这条
      飞轮，在技能包里是断的：管线退化成一次性的"输入图片 → OCR → 不知道产物在哪"。

    ★ 与 `preannotate` 那条通道的分工（**两条并行实现，不是重复**）：
      本命令走「已落盘产物 → 草稿」，读的是**已过 `seam.validate` 的那一份** ——
      人要验收的必须就是它，否则 verify 报绿的与人看到的会各说各话；
      界面上的 `/api/preannotations/generate` 走「structured/ → 重切分 → 草稿」，
      可以带 L0/L1/L2 的模型层。两者 tier 不同，各有其位（详见 `draft_bridge` docstring）。

    判定点只有一处：`draft_bridge.draft_exit_code`（本函数**不重判**）。
    """
    import draft_bridge as DB

    pid = a.profile
    plans_dir = Path(a.plan_dir) if a.plan_dir else DB.DEFAULT_PLANS_DIR
    try:
        plan = DB.load_plan(pid, plans_dir)
    except FileNotFoundError:
        return _fail(EXIT_MISSING, f"方案不存在：{DB.plan_path(pid, plans_dir)}",
                     f"先跑 `chronicles plan --profile {pid}`（契约门面由它提供）。",
                     a.as_json)

    stems, page_source = DB.resolve_stems(
        pid, plan, stem=a.stem, pages=a.pages, project=a.project_id,
        results_dir=a.results_dir)
    if not stems:
        return _fail(
            EXIT_MISSING,
            f"没有可桥的产物（page_source={page_source}）",
            (f"这个档案在 {Path(a.results_dir or DB.DEFAULT_RESULTS_DIR)} 下还没有产物 —— "
             f"先跑 `chronicles exec --profile {pid} --pages <页目录>`。"),
            a.as_json)

    r = DB.bridge_stems(stems, plan, profile_id=pid, results_dir=a.results_dir,
                        drafts_dir=a.out_dir, structured_dir=a.structured_dir,
                        outbox_dir=a.outbox, archive_dir=a.archive_dir,
                        overwrite=a.overwrite, dry_run=a.dry_run)
    rc = int(r.get("exit_code", EXIT_INTERNAL))

    urls: List[str] = []
    try:
        # ★ `existing_differs` 也要给 URL —— 那些页**本来就可裁决**（草稿来自别的通道），
        #   本命令的职责是"让每页产物都能被人看到"，不是"非要用我的版本"。
        urls = DB.annotate_urls([p["stem"] for p in r["pages"]
                                 if p.get("status") in ("written", "unchanged",
                                                        "existing_differs")],
                                outbox_dir=a.outbox)
    except Exception:  # noqa: BLE001 —— 指路信息是增益，坏了不改判定
        urls = []

    if not a.as_json:
        _mirror(DB.format_human, r, as_json=False)
        for u in urls[:10]:
            print(f"  ▸ {u}")
        return rc

    payload = {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "draft", "schema_version": SCHEMA_VERSION,
        "profile": pid, "plan": str(DB.plan_path(pid, plans_dir)),
        "page_source": page_source,
        "results_dir": str(Path(a.results_dir or DB.DEFAULT_RESULTS_DIR)),
        "drafts_dir": r.get("drafts_dir"),
        "n_pages": r.get("n_pages"), "n_written": r.get("n_written"),
        # ★ 「新建」与「替换」拆开给 —— 替换是本命令唯一的**破坏性**动作
        #   （顶掉一份既有草稿），只报 `n_written` 会把它藏进"新写"里。
        #   实测踩过：f7 报"新写 9 页"，实为替换 9 页（原文已归档）。
        "n_created": r.get("n_created"), "n_replaced": r.get("n_replaced"),
        "n_unchanged": r.get("n_unchanged"),
        # ★ 有草稿但与产物不一致的页数 —— **不是失败**（那页本来就可裁决），
        #   但要显式给，否则「26 页有草稿」会被误读成「26 页都换了」。
        "n_existing_differs": r.get("n_existing_differs"),
        "n_skipped": r.get("n_skipped"), "status": r.get("status"),
        "overwrite": bool(a.overwrite), "dry_run": bool(a.dry_run),
        "pages": r.get("pages"),
        "annotate": {"base_url": DB.base_url(), "urls": urls, "n_urls": len(urls)},
        "advice": DB.advice_for(r), "next": DB.advice_for(r),
        "exit_meaning": EXIT_MEANING.get(rc),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _mirror(DB.format_human, r, as_json=True)
    return rc


def cmd_sample(a: argparse.Namespace) -> int:
    """跑批产物随机抽 n 页交人复核（七步流程第 7 步的抽样口）。

    判定点只有一处：`draft_bridge.sample_exit_code`（本函数不重判）。
    抽样是纯选择：草稿桥接交给既有 `draft --stem`，本命令不合并它
    （桥有自己的对账与归档纪律，合并会让两套判定搅在一起）。
    """
    import draft_bridge as DB

    if a.n < 1:
        return _fail(EXIT_USAGE, f"--n 必须为正整数，得到 {a.n}",
                     "改成 `--n 3`（默认）或其它正整数。", a.as_json)

    r = DB.sample_stems(n=a.n, seed=a.seed, results_dir=a.results_dir,
                        gold_dir=a.gold_dir, outbox_dir=a.outbox)
    rc = int(r.get("exit_code", EXIT_INTERNAL))

    # ★ P11（2026-09-29）口 A：**先抽完、再回查** picked 页的画像 QA flags。
    #   随机抽选本身一字不动（仍走 `draft_bridge.sample_stems` 纯随机）——
    #   flags 若进了选择就是分层，会系统性低估整批通过率（明文纪律）。
    #   这里只把「抽中的页里有没有 QA 异常页」摆给人看：信封加 `picked_qa`，
    #   人读视图加一行「优先阅读」。不给 `--profile` ⇒ picked_qa 键缺席，
    #   输出与现状**逐字节不变**（零影响护栏）。
    picked_qa: Optional[List[Dict[str, Any]]] = None
    if getattr(a, "profile", None):
        qa_by_page = _corpus_qa_by_page(a.profile)
        if qa_by_page:
            picked_qa = [dict(stem=s, **qa_by_page[s])
                         for s in (r.get("picked") or []) if s in qa_by_page]

    if not a.as_json:
        if rc == EXIT_MISSING:
            print(f"sample: 产物池里没有可抽的页（{r['results_dir']}）。"
                  f"先跑 `chronicles exec`；若池被金标准抽空，本批已全量人工验证过。",
                  file=sys.stderr)
        else:
            print(f"抽样 {r['n_picked']}/{r['n_requested']}"
                  f"（池 {r['pool_size']}，已有人工金标准排除 {r['n_excluded_gold']}）"
                  f"  seed={r['seed']}")
            for s in r["picked"]:
                print(f"  ▸ {s}")
            if picked_qa:
                _detail = "、".join(
                    f"{x['stem']}({'/'.join(x['flags'])})" for x in picked_qa)
                print(f"⚠ QA 异常页在抽中列表，优先阅读：{_detail}")
            print("下一步：逐页 `chronicles draft --profile <档案> --stem <页名>`，"
                  "再 `chronicles ui --serve --stem <页名>` 交人复核。")
        return rc

    payload = {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "sample", "schema_version": SCHEMA_VERSION,
        "n_requested": r.get("n_requested"), "n_picked": r.get("n_picked"),
        "pool_size": r.get("pool_size"), "n_excluded_gold": r.get("n_excluded_gold"),
        "excluded_gold": r.get("excluded_gold"),
        "picked": r.get("picked"), "seed": r.get("seed"),
        "results_dir": r.get("results_dir"),
        "next": ("逐页桥草稿后交人复核：`chronicles draft --profile <pid> --stem <页名>` "
                 "→ `chronicles ui --serve --stem <页名>`。"
                 if rc == EXIT_OK else
                 "产物池为空：先 `chronicles exec --profile <pid> --project <栏目id>`；"
                 "若池被金标准抽空，本批已全量人工验证过。"),
        "exit_meaning": EXIT_MEANING.get(rc),
    }
    if picked_qa is not None:                    # 无 --profile / 无 corpus ⇒ 键缺席
        payload["picked_qa"] = picked_qa
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


def _fmt_adjudicate(r: Dict[str, Any], action: str) -> str:
    """裁决命令的人读视图 —— **只负责显示**（判定全在 `adjudicate.py`）。

    ⚠ 收口理由同 `_mirror` 的 docstring：显示层抛异常不得把 rc 0 变成 1。
    """
    L: List[str] = ["=" * 64]
    stem = r.get("stem")
    if action == "validation":
        # ★ 档案级读数的显示。**判据不在这里** —— 放行与否由 `batch_gate` 判，
        #   本段只把"登记了什么"如实摆出来（显示层不参与判定，同 `_mirror`）。
        pid = r.get("profile_id")
        L.append(f"回验读数 · {pid}")
        L.append("=" * 64)
        if not r.get("registered"):
            L.append("  状态      : **无记录**（`data/validation/` 里没有本档案）")
            L.append("  → 批量采纳对本档案处于**停用**状态（未验证不放行，"
                     "「没测过」≠「偏离小」）。")
            L.append("  解锁三步  : ① 标 3 页金标准（`chronicles next` 给标注入口）")
            L.append("             ② 跑离线对评得到读数（零 API，作用于已落盘产物）")
            L.append("             ③ 登记：`chronicles adjudicate validation "
                     f"--profile-id {pid} --verdict pass --metric value_aligned_iou50 "
                     "--value <读数>`")
        else:
            L.append("  结论      : **%s**" % r.get("verdict"))
            L.append("  度量      : %s = %s" % (r.get("metric"), r.get("value")))
            _pg = [str(x) for x in (r.get("pages") or [])]
            _nb = r.get("n_boxes")
            L.append("  金标准页  : %s（%s 页%s）" % (
                ", ".join(_pg) or "未记", len(_pg),
                " / %s 框" % _nb if _nb is not None else ""))
            L.append("  测量时刻  : %s" % (r.get("measured_at") or "未记"))
            if r.get("archived"):
                L.append("  旧记录    : 已归档 → %s" % r.get("archived"))
            if r.get("note"):
                L.append("  说明      : %s" % r.get("note"))
            if str(r.get("verdict")) == "pass":
                L.append("  → 档案级封禁已解除；某页能否批量采纳仍看它自己"
                         "（`chronicles adjudicate list --stem <页>` 会逐页给）。")
            else:
                L.append("  → 未达标，批量采纳**仍停用**：先修几何 / 修规则后"
                         "重新回验，或逐条裁决。")
        return "\n".join(L)
    if action == "list":
        st = r.get("stats") or {}
        by = st.get("by_confidence") or {}
        L.append(f"裁决 · 本页草稿 · {stem}")
        L.append("=" * 64)
        L.append("  草稿      : %s 条（%s）" % (
            st.get("total"), " / ".join(f"{k} {v}" for k, v in by.items())))
        L.append("  已裁决    : 采纳 %s · 忽略 %s · 待裁 %s" % (
            st.get("accepted"), st.get("rejected"), st.get("pending")))
        L.append("  人工层行数: %s" % st.get("manual_rows"))
        # ★ 判据已换成"档案级独立回验读数"（2026-09-17，用户批准）⇒ **显示也必须换**：
        #   仍旧把"几何来源 pipeline ⇒ 可用"讲给 agent 听，等于拿**已作废的判据**
        #   描述现状（造第二份真相）。几何来源降级为附注。
        _v = r.get("validation") or {}
        if _v:
            L.append("  回验读数  : %s · %s=%s（%s）" % (
                _v.get("verdict"), _v.get("metric"), _v.get("value"),
                _v.get("measured_at") or "未记时刻"))
        else:
            L.append("  回验读数  : **无记录**（未验证不放行）")
        if r.get("batch_safe"):
            L.append("  批量采纳  : **可用**（判据＝本档案回验达标）")
        else:
            L.append("  批量采纳  : **停用** —— %s" % r.get("batch_block_reason"))
        if r.get("geometry_source"):
            L.append("  几何来源  : %s（仅供参考，**非判据**）" % r.get("geometry_source"))
        e = r.get("estimate") or {}
        if e.get("items"):
            L.append("  操作估算  : 手工 %s 次 → 逐条 %s / 批量 %s 次（批量降 %s%%）" % (
                e.get("manual_ops"), e.get("draft_ops_each"), e.get("draft_ops_batch"),
                round((e.get("drop_batch") or 0) * 100, 1)))
        L.append("-" * 64)
        L.append("  %4s  %-12s %-7s %-9s %s" % ("#", "属性", "置信", "状态", "文本"))
        for it in r.get("entries") or []:
            txt = str(it.get("text") or "").replace("\n", " ")
            if len(txt) > 30:
                txt = txt[:30] + "…"
            L.append("  %4s  %-12s %-7s %-9s %s" % (
                it.get("idx"), str(it.get("attr") or "")[:12],
                str(it.get("confidence") or ""), str(it.get("status") or ""), txt))
        L.append("  → 逐条：`chronicles adjudicate accept --stem %s --idx N`；"
                 "批量：`--confidences high`（仅当批量采纳可用）" % stem)
    else:
        L.append(f"裁决 · {action} · {stem}")
        L.append("=" * 64)
        if r.get("action") == "batch" or action == "batch":
            L.append("  处理      : %s 条" % r.get("n"))
            if r.get("indices") is not None:
                L.append("  idx       : %s" % r.get("indices"))
            if r.get("skipped") is not None:
                L.append("  跳过      : %s 条（已裁决或不在范围内）" % r.get("skipped"))
            if r.get("failed"):
                L.append("  ⚠ 失败 idx: %s" % r.get("failed"))
        else:
            L.append("  结果      : %s" % r.get("status"))
            if r.get("rows"):
                L.append("  落库行数  : %s" % len(r.get("rows") or []))
            if r.get("removed") is not None:
                L.append("  移除行数  : %s" % r.get("removed"))
            if r.get("edited"):
                L.append("  （人工改过值/框/属性 ⇒ 天然的错例，已标记 edited）")
    return "\n".join(L)


def cmd_adjudicate(a: argparse.Namespace) -> int:
    """草稿裁决命令面（G2-①，2026-09-16）：
    `list / accept / reject / reset / batch / validation`。

    ★ 为什么必须有这一条：裁决此前**只有界面**能触发（`/annotate` + `/api/*`）。
      技能包里 agent 能跑 `draft` 造出草稿，却没有任何命令面把它裁决掉 ——
      飞轮第四段（裁决 → 回流改规则）在命令行上是断的。

    ★ **纯转调**（本函数只做「参数 → 调用 → 组织 JSON → 映射退出码」）：
      合法性、批量安全门、状态回写全部在 `adjudicate.py`。本文件里**不许**出现
      `_write_manual` / `atomic_write_json` 之类的写入实现 —— 那会造出第二份写入真相。

    ★ **批量安全门不可绕**：`batch --confidences` 是"机器的判断"，只有
      `--indices`（人逐条点过）才免于几何可信性检查 —— 而这一点也由
      `batch_adjudicate` 自己判定，**本函数不替它开口子**（不暴露 allow_untrusted）。

    ★ `validation`（2026-09-17，用户裁定 P2 落地）**与页无关**：批量采纳的准入
      判据已由「几何来源标签」换成「**该档案**有独立回验读数且达标」，而读数记在
      **档案**上 ⇒ 命令面必须给出**登记它的入口**，否则 `data/validation/` 永远为空、
      **全库批量采纳永久停用**。粒度是**档案**（不是页、不是期次），故本子命令
      **不接受 `--stem`**（给了就会把粒度误解回页级，回归原问题）。

    退出码：0 成功 · 2 面外拒答（`batch_gate` 不过，**正确行为**）· 3 缺料（没有草稿 /
            档案没有回验记录）· 4 校验不过（idx 越界 / box 格式错 / 文本为空 /
            `--verdict` 不在 pass|fail）· 1 内部错误（登记时归档或写盘失败）
            · 64 用法错误（选项缺失，或 `--value`/`--n-boxes` 类型解析不了）
    """
    import adjudicate as ADJ

    act = a.adj_cmd

    # ---------- validation（档案级回验读数：登记 / 查询）----------
    # ★ 位置必须在 `stem = ADJ.stem_of(a.stem)` **之前**：本动作不挂 `_adj_common`
    #   （见 docstring：粒度 = 档案，给它 `--stem` 会把粒度误导回页级），
    #   所以它没有 `a.stem` —— 放到后面会崩在 AttributeError（rc 被记成 1）。
    if act == "validation":
        pid = str(getattr(a, "profile_id", "") or "").strip()
        if not pid:
            return _fail(EXIT_MISSING, "缺 --profile-id：回验读数是**档案级**的",
                         "用法：`chronicles adjudicate validation --profile-id <prof_…> "
                         "--verdict pass --metric value_aligned_iou50 --value <读数>`；"
                         "不带 `--verdict` 则只查询现有读数、不写盘。", a.as_json)
        _vdir = getattr(a, "validation_dir", None)
        _verdict = getattr(a, "verdict", None)
        if _verdict is None:
            # 查询模式（与 `profile show` 同构）：不写盘、不改任何状态。
            _rec = ADJ.load_validation(pid, _vdir)
            if _rec is None:
                return _fail(EXIT_MISSING,
                             f"档案 {pid} 没有回验记录（`data/validation/` 无此文件）",
                             "批量采纳对本档案因此是**停用**的（未验证不放行）。"
                             "解锁：标注 3 页金标准 → 跑一次离线对评（零 API）→ "
                             f"`chronicles adjudicate validation --profile-id {pid} "
                             "--verdict pass --metric value_aligned_iou50 --value <读数>`。",
                             a.as_json, extra={"profile_id": pid, "registered": False})
            r = {"profile_id": pid, "registered": True,
                 "path": str(ADJ.validation_path(pid, _vdir))}
            r.update(_rec)
        else:
            _wr = ADJ.write_validation(
                pid, _verdict, metric=getattr(a, "metric", None),
                value=getattr(a, "value", None), n_boxes=getattr(a, "n_boxes", None),
                pages=_split_csv(getattr(a, "pages", "") or ""),
                note=getattr(a, "note", "") or "", validation_dir=_vdir,
                archive_dir=getattr(a, "archive_dir", None))
            if not _wr.get("ok"):
                # ★ 档位由**数据层**给的 `error_kind` 决定 —— 命令面**不重判**
                #   `verdict` 的域（那会是第二份真相），也不去匹配错误字符串。
                _kind = str(_wr.get("error_kind") or "")
                _code = EXIT_INVALID if _kind == "invalid" else EXIT_INTERNAL
                _nxt = ("`--verdict` 只接受 pass / fail —— **没有第三种**："
                        "拿不准就是 fail（`--value` 填不上也可以只记 pass/fail）。"
                        if _kind == "invalid" else
                        "检查 `_archive/` 与 `data/validation/` 是否可写；"
                        "旧记录已在写入前归档，失败即拒绝覆盖，故原记录未被破坏。")
                return _fail(_code, str(_wr.get("error") or "登记失败"), _nxt,
                             a.as_json,
                             extra={"profile_id": pid, "error_kind": _kind})
            r = {"profile_id": pid, "registered": True,
                 "path": _wr.get("path"), "archived": _wr.get("archived") or ""}
            r.update(_wr.get("doc") or {})
    else:
        stem = ADJ.stem_of(a.stem)
        drafts_dir, status_path = a.drafts_dir, a.status_path
        manual_dir = a.manual_dir
        image_name = a.image or f"{stem}.png"

        # ---------- list ----------
        if act == "list":
            st = ADJ.page_state(stem, image_name=image_name, drafts_dir=drafts_dir,
                                status_path=status_path, manual_dir=manual_dir,
                                structured_dir=a.structured_dir, drift_dir=a.drift_dir,
                                image_dir=getattr(a, "image_dir", None),
                                validation_dir=getattr(a, "validation_dir", None))
            if not st.get("has_draft"):
                return _fail(EXIT_MISSING, f"没有草稿：{stem}",
                             f"先跑 `chronicles draft --profile <档案id> --stem {stem}`"
                             f"（产物 → 草稿的人面桥），或用生成口：建服务后在"
                             f"`/annotate` 页点生成。", a.as_json)
            if not a.as_json:
                _mirror(lambda r: _fmt_adjudicate(r, "list"), st, as_json=False)
                return EXIT_OK
            payload = {"ok": True, "exit": EXIT_OK, "subcommand": "adjudicate",
                       "action": "list", "schema_version": SCHEMA_VERSION,
                       "exit_meaning": EXIT_MEANING.get(EXIT_OK)}
            payload.update(st)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            _mirror(lambda r: _fmt_adjudicate(r, "list"), st, as_json=True)
            return EXIT_OK

        # ---------- batch ----------
        if act == "batch":
            indices = None
            if a.indices:
                try:
                    indices = [int(x) for x in _split_csv(a.indices)]
                except ValueError:
                    return _fail(EXIT_INVALID, f"--indices 里不是整数：{a.indices!r}",
                                 "写成逗号分隔的序号，如 `--indices 1,3,7`。", a.as_json)
                if not indices:
                    return _fail(EXIT_INVALID, "--indices 给空了",
                                 "要么给序号（人的判断），要么给 --confidences（按机器分层）。",
                                 a.as_json)
            confs = tuple(_split_csv(a.confidences)) if a.confidences else ("high",)
            r = ADJ.batch_adjudicate(stem, image_name, action=a.action,
                                     confidences=confs, indices=indices,
                                     drafts_dir=drafts_dir, status_path=status_path,
                                     manual_dir=manual_dir, archive_dir=a.archive_dir,
                                     structured_dir=a.structured_dir, drift_dir=a.drift_dir,
                                     image_dir=getattr(a, "image_dir", None),
                                     validation_dir=getattr(a, "validation_dir", None))
            if not r.get("ok"):
                err = str(r.get("error") or "批量裁决失败")
                # ★ 安全门不过 ⇒ 2（面外拒答）：这是**数据层的正确拒绝**，
                #   人话原因已在 `error` 里 ⇒ 原样转达，别让 agent 去"修"。
                if r.get("blocked"):
                    return _fail(EXIT_OUT_OF_SCOPE, err,
                                 "这是设计好的拦阻（红线二：固化必须由人的判断触发）。"
                                 "逐条裁决即可；若是版式漂移，先处理 variant。",
                                 a.as_json,
                                 extra={"blocked": r.get("blocked"),
                                        "batch_safe": r.get("batch_safe"),
                                        "geometry_source": r.get("geometry_source"),
                                        "validation": r.get("validation"),
                                        "batch_block_reason": r.get("batch_block_reason")})
                # ★ 其余档位由**数据层**的 `error_kind` 决定（同 `validation` 先例：
                #   命令面只映射，不匹配错误字符串、不重判）。旧行为把一切非 blocked
                #   失败兜底成 3「先跑 draft」—— 写盘失败也会被指向重跑 draft，
                #   而两者的下一步动作完全不同（2026-09-24 审查修正）。
                _kind = str(r.get("error_kind") or "")
                if _kind == "usage":
                    return _fail(EXIT_USAGE, err,
                                 "批量只接受 accept / reject；reset 请逐条。", a.as_json)
                if _kind == "missing":
                    return _fail(EXIT_MISSING, err, "先跑 `chronicles draft` 造草稿。",
                                 a.as_json)
                return _fail(EXIT_INTERNAL, err,
                             "写盘 / 状态保存失败（不是缺料）：检查目标目录可写性；"
                             "重跑 draft 无济于事。", a.as_json)
        else:
            # ---------- accept / reject / reset ----------
            try:
                idx = int(a.idx)
            except (TypeError, ValueError):
                return _fail(EXIT_INVALID, f"--idx 必须是整数：{a.idx!r}",
                             "看 `chronicles adjudicate list --stem " + stem + "` 取序号。",
                             a.as_json)
            box = None
            # ★ `--text/--box/--attr` 只注册在 accept 上（reject/reset 带它们语义不清），
            #   故此处必须 getattr 兜底 —— 直接取属性会让 reject/reset 崩在 AttributeError。
            _box_arg = getattr(a, "box", None)
            if _box_arg:
                parts = _split_csv(_box_arg)
                try:
                    if len(parts) != 4:
                        raise ValueError(f"要 4 个数，实得 {len(parts)}")
                    box = [float(x) for x in parts]
                except ValueError as e:
                    return _fail(EXIT_INVALID, f"--box 解析失败：{e}（输入 {_box_arg!r}）",
                                 "格式：`--box x1,y1,x2,y2`（像素，左上右下）。", a.as_json)
            # 「没有草稿」是缺料（3）；「草稿里没这个 idx」是校验不过（4）。
            # reset 例外：它按 ai_ref 清残留，不需要草稿还在。
            if act != "reset" and not ADJ.load_drafts(stem, drafts_dir):
                return _fail(EXIT_MISSING, f"没有草稿：{stem}",
                             f"先跑 `chronicles draft --profile <档案id> --stem {stem}`。",
                             a.as_json)
            r = ADJ.adjudicate(stem, image_name, idx, act,
                               text=getattr(a, "text", None), box=box,
                               attr=getattr(a, "attr", None), drafts_dir=drafts_dir,
                               status_path=status_path, manual_dir=manual_dir,
                               archive_dir=a.archive_dir)
            if not r.get("ok"):
                return _fail(EXIT_INVALID, str(r.get("error") or "裁决失败"),
                             f"用 `chronicles adjudicate list --stem {stem}` 核对序号与草稿内容。",
                             a.as_json)

    if not a.as_json:
        _mirror(lambda x: _fmt_adjudicate(x, act), r, as_json=False)
        return EXIT_OK

    payload = {"ok": True, "exit": EXIT_OK, "subcommand": "adjudicate",
               "action": act, "schema_version": SCHEMA_VERSION,
               "exit_meaning": EXIT_MEANING.get(EXIT_OK)}
    payload.update(r)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _mirror(lambda x: _fmt_adjudicate(x, act), r, as_json=True)
    return EXIT_OK


def cmd_next(a: argparse.Namespace) -> int:
    """下一步**去哪做**（人面入口，2026-09-16 用户裁定②，裁定已于同日修订）。

    ★ 为什么需要它：整套管线里唯一必须由人做的那一步（创造金标准页 / 验收草稿），
      此前在命令面里**没有任何东西指向它** —— 跑完 `ocr` 之后该打开哪个页面、
      `exec` 之后草稿在哪看，全靠人自己拼 URL。用户原话：

        「如果我现在换一组材料，换成另一批材料，从一开始就没有金标准页，
          那我要在哪里创造金标准页？」

    ★ 服务：本命令**仍然只探测、不起**（保持"看下一步"这个动作轻）；但
      **「不起服务」已不再是全局裁定** —— 用户 2026-09-16 修订：
      「同意加 `chronicles ui [--serve]`…**允许在运行管线中的必要节点起服务**」。
      要起服务走 `chronicles ui --serve`（唯一实现 → `ui_cli.serve`）。

    ★ 判定只认「进过管线」的页：`outbox/` 里躺着一批探测残留（`_api_check.png`…），
      它们天然没有 OCR；若计入判定，本命令会让人去"OCR 一堆测试图"，
      而真正的入口（那批已 OCR、等人标的页）反被藏起来。详见 `next_step`。

    判定点只有一处：`next_step.run_next`（本函数**不重判**）。
    """
    import next_step as NS

    r = NS.run_next(profile=a.profile, project_id=a.project_id, stem=a.stem,
                    limit=a.limit, gold_dir=a.gold, drafts_dir=a.drafts_dir,
                    structured_dir=a.structured_dir, outbox_dir=a.outbox)
    rc = int(r.get("exit_code", EXIT_INTERNAL))

    if not a.as_json:
        _mirror(NS.format_human, r, as_json=False)
        return rc

    payload = dict(r)
    payload.update({"subcommand": "next", "schema_version": SCHEMA_VERSION,
                    "exit": rc,
                    "advice": NS.advice_for(r),
                    "next_command": NS.next_command(r),
                    "exit_meaning": EXIT_MEANING.get(rc)})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


def cmd_ui(a: argparse.Namespace) -> int:
    """界面通道：给标注页地址；`--serve` 起服务（G1 落地，2026-09-16）。

    ★★ 这是**裁定变更**的落点。旧裁定「给地址，不替人起服务」已作废；
      用户 2026-09-16 原话：

        「①G1：同意加 `chronicles ui [--serve]`，且老裁定已经明显阻碍到了用户
          对产品的顺利体验，我决定修改该裁定，**允许在运行管线中的必要节点起服务**。」

    ★ 与 `next` 的分工（不是重复实现）：
      · `next`  = **看下一步在哪**（按页分类，给出每页该去的地址；不起服务）
      · `ui`    = **把界面交到人手里**（地址 / 起服务 / 某一页的直达 URL）

    ★ 起服务**必须先探测、能复用就复用**（旧裁定里仍有效的那一半）：
      技能包面（5001）已在跑就不重复起。

    ★★ **统一裁定**（2026-09-30）：本通道**只有**技能包自有交互面
      （`skill/chronicles-app/annotate_server.py`，端口 5001）。桌面壳的后端
      （app.py，5000）由桌面壳自己拉起/重启，本通道**不再代拉、不再探测它**——
      本机与分发包行为完全一致，没有「本机落壳、分发落 skill」的分叉。
      （2026-09-16 批次 C 的 `--surface auto|app|skill` 开关随本裁定移除；
       该开关修掉的「分发断点」由"恒 skill 面"永久保住。）

    判定点只有一处：`ui_cli.ui_exit_code`（本函数**不重判**）。
    """
    import ui_cli as UI

    # ★ 参数 → kwargs **只有一份**（`ui_cli.kwargs_from_args`，§60）：
    #   原先这里手抄了 5 个字段，加 `--surface` 这种新参数时会漏抄 ⇒ 静默回落默认值。
    r = UI.run_ui("ui", **UI.kwargs_from_args(a))
    rc = int(r.get("exit_code", EXIT_INTERNAL))

    if not a.as_json:
        _mirror(UI.format_human, r, as_json=False)
        return rc

    payload = dict(r)
    payload.update({"subcommand": "ui", "schema_version": SCHEMA_VERSION,
                    "exit": rc,
                    "advice": UI.advice_for(r),
                    "exit_meaning": EXIT_MEANING.get(rc)})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


def cmd_verify(a: argparse.Namespace) -> int:
    """自校验：核对一份 result.json（可给 plan 做对位判据）。"""
    res_p = Path(a.result)
    if not res_p.exists():
        return _fail(EXIT_MISSING, f"结果不存在：{res_p}",
                     "先跑 `chronicles exec --profile <id>`，或核对路径。", a.as_json)
    if a.plan and not Path(a.plan).exists():
        return _fail(EXIT_MISSING, f"plan 不存在：{a.plan}",
                     "去掉 --plan 只核内部自洽，或给出正确的 plan 路径。", a.as_json)

    import seam as SEAM

    res = SEAM.load_json(res_p)
    plan = SEAM.load_json(a.plan) if a.plan else None
    rep = SEAM.validate(res, plan=plan, resolve_source=not a.no_source)

    _mirror(lambda _rep: SEAM.format_report(_rep, title=res_p.name), rep, a.as_json)

    rc = SEAM.verify_exit_code(rep)             # ★ 判定点在数据层，包装层不重判
    _emit(a.as_json, {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "verify", "schema_version": SCHEMA_VERSION,
        "result": str(res_p), "plan": a.plan or None,
        "errors": rep.get("errors"), "metrics": rep.get("metrics"),
    })
    return rc


def cmd_eval_iou(a: argparse.Namespace) -> int:
    """层 4：几何对评（**零 API**）—— P5 第 ④ 道门「回验读数」的唯一来源。

    ★ 为什么必须有这一条：`adjudicate.VALIDATION_METRICS` 里登记的度量名此前
      **只有名字、没有实现**（原文：「只登记名字，不在此实现度量 —— 度量实现属
      离线对评工具」），也没有任何命令面能跑出那个数。缺了这一条，
      `data/validation/` 永远空着 ⇒ 第 ④ 道门是一道**死门**（全库批量采纳永久停用）。

    ★ **纯转调**：编排、口径、判定全在 `align_eval.run()`；本函数只做
      「参数 → 调用 → 组织 JSON → 映射退出码」。口径（分母取金标准侧、IoU 阈值
      0.5）**不在本文件里重述**，以免出现第二份"什么算命中"的真相。

    ★ 两种形态：给 `--stem` = 单页读数；不给 = **留一交叉验证**（每折留出一页学
      契约，在该页上测）⇒ 测试页未参与学习，独立性不需要新标注页就能满足。

    退出码：0 成功 · 3 缺料（无契约 / 无 records / 无金标准 / 入选源页 < 3）
            · 64 用法错误 · 1 内部错误
    """
    import align_eval as AE

    argv = ["--profile-id", a.profile_id]
    if a.stem:
        argv += ["--stem", a.stem]
    if a.oracle:
        argv += ["--oracle"]
    if a.run_dir:
        argv += ["--run-dir", a.run_dir]
    for opt, val in (("--ann-dir", a.ann_dir),
                     ("--structured-dir", a.structured_dir),
                     ("--outbox", a.outbox),
                     ("--divergence-dir", a.divergence_dir),
                     ("--contracts-dir", a.contracts_dir)):
        if val:
            argv += [opt, val]
    if a.iou is not None:
        argv += ["--iou", str(a.iou)]

    rc, payload = AE.run(argv)
    _mirror(AE.format_human, payload, a.as_json)
    _emit(a.as_json, {"subcommand": "eval-iou", "exit": rc, **payload})
    return rc


def cmd_map(a: argparse.Namespace) -> int:
    """方案结构图（IR / SVG）。"""
    import seam_map as SM

    argv: List[str] = []
    if a.profile:
        argv += ["--profile", a.profile]
    if a.svg:
        argv += ["--svg", a.svg]
    if a.plan:
        argv += ["--plan", a.plan]
    if a.gold:
        argv += ["--gold", a.gold]
    if a.gold_profile:
        argv += ["--gold-profile", a.gold_profile]

    # 底层 `--json` 的语义是「把 IR 写到该路径」，与包装层的「输出到 stdout」不同：
    # 有 --ir 就直接用它的路径；否则借一个临时文件，读完即清。
    tmp: Optional[str] = None
    ir_path = a.ir
    if a.as_json and not ir_path:
        fd, tmp = tempfile.mkstemp(prefix="chronicles_ir_", suffix=".json")
        os.close(fd)
        ir_path = tmp
    if ir_path:
        argv += ["--json", ir_path]

    try:
        rc = _run_entry(SM.main, argv, a.as_json)
        ir = _read_json(Path(ir_path)) if ir_path else None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    rc = SM.map_exit_code(rc, ir, as_json=a.as_json)   # ★ 判定点在数据层
    _emit(a.as_json, {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "map", "schema_version": SCHEMA_VERSION,
        "profile": a.profile or None, "svg": a.svg or None,
        "ir_artifact": (a.ir or None), "ir": ir,
    })
    return rc


def cmd_ocr(a: argparse.Namespace) -> int:
    """OCR 环节：图像 → `data/structured/*.json`（第 0 步补完，2026-09-14）。

    ⚠ 与 `triage` 同构：OCR 环节**没有可转发的既有 CLI**（`preannotate_gen` 是全链路
      编排、不是 OCR 入口），故本函数**直接取用** `ocr_cli.run_ocr` —— 它同时产出结果
      与 `exit_code`（唯一判定点），本函数**不重判**。

    ★ 粒度（用户 2026-09-14 明确「注意之前改造的粒度问题」）：默认走 P-OPT-7 的批量档
      （`OCR_BATCH_PREFETCH = 100`），**实际粒度读数随结果返回**（`engine` / `batch` 字段），
      让「并发多少」可见。`submit_interval = 0.25 s` 承重，不提供关闭开关。
    """
    import ocr_cli as OC

    pf = a.prefetch
    if a.no_prefetch:
        pf = 0
    r = OC.run_ocr(images=a.images, out_dir=a.out, limit=a.limit,
                   prefetch=pf, parallel=a.parallel, dry_run=a.dry_run)
    rc = int(r.get("exit_code", EXIT_INTERNAL))

    if not a.as_json:
        _mirror(OC.format_human, r, as_json=False)
        return rc

    payload = {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "ocr", "schema_version": SCHEMA_VERSION,
        "n_images": r.get("n_images"), "n_ok": r.get("n_ok"),
        "n_failed": r.get("n_failed"),
        "out_dir": r.get("out_dir"), "dry_run": r.get("dry_run"),
        "engine": r.get("engine"), "batch": r.get("batch"),
        "images": r.get("images"), "items": r.get("items"),
        "errors": r.get("errors"),
        "elapsed_ms": r.get("elapsed_ms"),
        "advice": r.get("error") or OC.advice_for(r),
        "next": OC.advice_for(r),
        "exit_meaning": EXIT_MEANING.get(rc),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _mirror(OC.format_human, r, as_json=True)
    return rc


def cmd_import(a: argparse.Namespace) -> int:
    """图片导入（用户 2026-09-14 第 2 条）：外部图像 → `inbox/`。

    ⚠ 与 `triage` / `ocr` 同构：本环节**没有可转发的既有 CLI** —— GUI 的批量导入是
      「复制 + 等 OCR + 归栏目」的**常驻进程**任务，而命令面不能依赖别的进程在跑
      （§4.2：一次调用必须自己跑完）。故**直接取用** `import_cli.run_import` ——
      它同时产出结果与 `exit_code`（唯一判定点），本函数**不重判**。

    ★ 它与剪贴板通道的关系：剪贴板监听是**操作系统事件驱动**的常驻进程，天生不属于
      命令面（保持原样）；本命令补的是「**用户主动给一批材料**」这条渠道。
      两者落位、命名、去重**同一套口径**（`image_naming` + `batch_import.pixel_hash`）。
    """
    import import_cli as IC

    r = IC.run_import(images=a.images, to=a.to, recursive=not a.no_recursive,
                      dry_run=a.dry_run, limit=a.limit, pdf_dpi=a.pdf_dpi)
    rc = int(r.get("exit_code", EXIT_INTERNAL))
    if not a.as_json:
        _mirror(IC.format_human, r, as_json=False)
        return rc
    payload = {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "import", "schema_version": SCHEMA_VERSION,
        "to": r.get("to"), "dry_run": r.get("dry_run"),
        "n_scanned": r.get("n_scanned"), "n_images": r.get("n_images"),
        "n_pdf_skipped": r.get("n_pdf_skipped"), "n_unsupported": r.get("n_unsupported"),
        # ★ WP-1（2026-09-15）：PDF 入库读数。同一条**逐字段投影**纪律 ——
        #   底层加了字段若不在此列出，agent 拿到的 JSON 里就没有这个信号。
        #   `n_pdf_skipped` 语义已收窄为「**因缺依赖而跳过**」，不再等同"扫到的 PDF 份数"。
        "n_pdf_pages": r.get("n_pdf_pages"), "n_pdf_rendered": r.get("n_pdf_rendered"),
        "n_pdf_failed": r.get("n_pdf_failed"), "n_pdf_no_dep": r.get("n_pdf_no_dep"),
        "pdf_dpi": r.get("pdf_dpi"),
        "n_planned": r.get("n_planned"), "n_copied": r.get("n_copied"),
        "n_skipped_dup": r.get("n_skipped_dup"), "n_failed": r.get("n_failed"),
        "n_renamed": r.get("n_renamed"),
        # ★ 幂等读数（§78）：**必须在这里显式列出** —— 本信封是**逐字段投影**，
        #   底层新增字段不会自动穿透，agent 拿到的 JSON 就会缺这几个信号。
        "n_existing": r.get("n_existing"),
        "n_skipped_existing": r.get("n_skipped_existing"),
        "dedup_scope": r.get("dedup_scope"),
        "dedup_seconds": r.get("dedup_seconds"),
        "planned": r.get("planned"), "errors": r.get("errors"),
        "advice": r.get("error") or IC.advice_for(r),
        "next": IC.advice_for(r),
        "exit_meaning": EXIT_MEANING.get(rc),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _mirror(IC.format_human, r, as_json=True)
    return rc


def _corpus_summary(learned_path) -> Optional[dict]:
    """已落盘的 learned_<pid>.json → 材料画像摘要（材料画像 scan · 2026-09-29）。

    ★ 与本模块一贯口径一致：**只读回已落盘的产物**，不重算、不 import 学习层。
      learned 缺 corpus 块 / 文件读不动 → `None`（键缺席语义，与既有 payload 风格一致）。
      ★ `learned_path` 为 None（list / show-miss 等 payload 无文件可指的动作）→ `None`，
      不进 Path()——否则 `Path(None)` TypeError 打穿整个 cmd_profile（全量回归 4 红实证）。
    """
    if not learned_path:
        return None
    try:
        p = Path(learned_path)
        if not p.exists():
            return None
        learned = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    c = learned.get("corpus") if isinstance(learned, dict) else None
    if not isinstance(c, dict) or not c:
        return None
    return {
        "n_pages": c.get("n_pages"),
        "n_constants_added": len(c.get("constants_added") or []),
        "n_candidates": len(c.get("constants_candidates") or []),
        "n_flagged": c.get("qa_n_flagged"),
    }


def _corpus_qa_by_page(pid: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """档案 id → {stem: {flags, lines, chars}}（P11 QA 消费口的**唯一取数口**，2026-09-29）。

    ★ 唯一读口 = `rule_learn.load_corpus`（同 `rule_report` 的先例：第二消费方
      共用第一读函数，不另写第二套解析）。无 corpus / 读不动 → `None`；
      页无登记或 flags 为空 → 该 stem 不进映射。
    ★ 口 A / 口 B 都只是**呈现层**：flags 绝不进抽样选择、执行顺序或任何判定
      （分层会系统性低估整批通过率，draft_bridge.sample_stems 明文纪律）。
    """
    try:
        import rule_learn as RL
        corpus = RL.load_corpus(pid)
    except Exception:  # noqa: BLE001 —— 呈现层增益，坏了不改判定（同 _corpus_summary）
        return None
    if not isinstance(corpus, dict):
        return None
    qa = corpus.get("qa") or {}
    pages = qa.get("pages") or []
    out: Dict[str, Dict[str, Any]] = {}
    for p in pages:
        if isinstance(p, dict) and p.get("flags"):
            out[str(p.get("page"))] = {
                "flags": [str(f) for f in p["flags"]],
                "lines": p.get("lines"),
                "chars": p.get("chars"),
            }
    return out or None


def cmd_profile(a: argparse.Namespace) -> int:
    """**档案**命令面（技能包第 4 步 · 2026-09-15）：建档 / 查档 / 加属性 / **学规则**。

    ⚠ 与 `triage` / `ocr` / `import` 同构：本函数**只取用** `profile_cli.run_profile`
      产出的 `exit_code`（**唯一判定点**），**不重判**。

    ★ 为什么「学规则」必须在这个面上 —— 分诊判据（锚词）的真实来源是

        manual_annotations/*.jsonl
          → rule_learn.learn_and_save        → rules_data/learned_<pid>.json
          → rule_split.build_spec(learned, profile)  → spec.anchors
          → page_triage.count_anchors        → 分诊判据

      **标注完金标准页 ≠ triage 能用**：中间少这一步，`anchors` 就是空的 ⇒
      `has_anchors=False` ⇒ 整批判 `unmeasurable`（退出码 3）。
    """
    import profile_cli as PC

    r = PC.run_profile(a.profile_cmd, **PC.kwargs_from_args(a))
    rc = int(r.get("exit_code", EXIT_INTERNAL))
    if not a.as_json:
        _mirror(PC.format_human, r, as_json=False)
        return rc
    payload = {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "profile", "action": r.get("action"),
        "schema_version": SCHEMA_VERSION,
        "profile": r.get("profile"), "profiles": r.get("profiles"),
        "n_profiles": r.get("n_profiles"),
        "attrs": r.get("attrs"), "reused": r.get("reused"),
        "dry_run": r.get("dry_run"),
        "profile_id": r.get("profile_id"), "summary": r.get("summary"),
        "n_gold_pages": r.get("n_gold_pages"), "n_gold_rows": r.get("n_gold_rows"),
        "gold_pages": r.get("gold_pages"), "learned_path": r.get("learned_path"),
        "corpus": _corpus_summary(r.get("learned_path")),
        "report_md": r.get("report_md"),
        "overrides": r.get("overrides"),
        "overrides_path": r.get("overrides_path"),
        # 规则确认态（2026-09-30）：rules 显示 / confirm 落盘 —— 逐字段投影，显式穿透
        "confirmation": r.get("confirmation"),
        "confirm_path": r.get("confirm_path"),
        "error": r.get("error"), "errors": r.get("errors"),
        "advice": PC.advice_for(r), "next": PC.advice_for(r),
        "exit_meaning": EXIT_MEANING.get(rc),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _mirror(PC.format_human, r, as_json=True)
    return rc


def cmd_project(a: argparse.Namespace) -> int:
    """**专项栏目**命令面（技能包第 4 步 · 2026-09-15）：建栏目 / 传模板建档案 / 归栏目。

    ★「归栏目」是闭合必需的一环：`triage --project` 读
      `data/project_assignments.json`，而 `import` **不写归属** ⇒ 只 import
      不 assign 的话，`--project` 一页都取不到（静默断裂，见 `project_cli` 文档）。

    ★「传模板」是 `app.py` 那段 60 行编排的**同一份实现**
      （`project_cli.apply_template_style`），GUI 路由已改为转调它。

    ⚠ 同构：只取用 `project_cli.run_project` 的 `exit_code`，不重判。
    """
    import project_cli as PJ

    r = PJ.run_project(a.project_cmd, **PJ.kwargs_from_args(a))
    rc = int(r.get("exit_code", EXIT_INTERNAL))
    if not a.as_json:
        _mirror(PJ.format_human, r, as_json=False)
        return rc
    payload = {
        "ok": rc == EXIT_OK, "exit": rc,
        "subcommand": "project", "action": r.get("action"),
        "schema_version": SCHEMA_VERSION,
        "project": r.get("project"), "projects": r.get("projects"),
        "n_projects": r.get("n_projects"),
        "reused": r.get("reused"), "dry_run": r.get("dry_run"),
        "n_assigned": r.get("n_assigned"), "n_missing": r.get("n_missing"),
        "assigned": r.get("assigned"), "missing": r.get("missing"),
        "n_images": r.get("n_images"), "images": r.get("images"),
        "label": r.get("label"), "attrs": r.get("attrs"),
        "profile": r.get("profile"), "profile_error": r.get("profile_error"),
        "error": r.get("error"),
        "advice": PJ.advice_for(r), "next": PJ.advice_for(r),
        "exit_meaning": EXIT_MEANING.get(rc),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    _mirror(PJ.format_human, r, as_json=True)
    return rc


def _parse_box(s: str):
    """`x1,y1,x2,y2` → `[x1,y1,x2,y2]`（容忍全角逗号）。"""
    parts = [p.strip() for p in str(s).replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("box 必须是 4 个数字：x1,y1,x2,y2")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"box 含非数字：{s!r}")


def _split_csv(s: str):
    """逗号 / 空白分隔的列表参数 → `List[str]`（容忍全角逗号，与 `_parse_box` 同一容忍度）。

    ★ 2026-09-24 提为模块级：原是 `cmd_adjudicate` 里的局部闭包，与 `_parse_box`
      构成「同一文件两份解析器、全角容忍度还不一致」（审查发现）；且 AST 纯度
      护栏会把 `str.replace` 误判成裸文件 IO `os.replace` —— 护栏不许为调用方放水，
      所以是**解析器上提**，不是护栏放宽。
    """
    import re
    return [x for x in re.split(r"[,\s]+", (s or "").replace("，", ",").strip()) if x]


def cmd_annotate(a: argparse.Namespace) -> int:
    """人工标注（金标准页）的命令面（用户 2026-09-14 第 1 条）。

    ★ **为什么 agent 需要这条通道，而不是自己标**：金标准页的一行是
      `{box, text, attr, profile, …}`，其中 `box` 是**原图像素坐标** ——
      agent 读得懂文本、判得了属性，但它**画不出框**。所以本命令面给的是
      「**提框 + 由人定属性**」的通道：

          annotate lines           列出该页 OCR 行（编号 + 文本 + 框）← agent 拿去转述
          annotate add --line N …  人定了属性 → 机器落盘（source: manual）

      判属性的是**人**，落盘的是**机器** —— 设计稿 §5.3「核心交互 = 裁决，不是标注」
      的可执行形态。落盘格式与界面 `/annotate` **逐字同源**（`manual_annotate`）。
    """
    import manual_annotate as MA

    sub = getattr(a, "annotate_cmd", None)

    if sub == "lines":
        rows = MA.page_lines(a.image, structured_dir=a.structured_dir,
                             outbox_dir=a.outbox)
        if not rows:
            return _fail(EXIT_MISSING, f"该页取不到 OCR 行：{a.image}",
                         "先跑 `chronicles ocr --images <该页所在目录>`（或核对 --image 名称）。",
                         a.as_json)
        lines = [{"index": i, "text": r.get("text") or "", "box": r.get("box")}
                 for i, r in enumerate(rows, 1)]
        if not a.as_json:
            for it in lines:
                print(f"{it['index']:>3}. {it['text']}   box={it['box']}")
            return EXIT_OK
        print(json.dumps({
            "ok": True, "exit": EXIT_OK, "subcommand": "annotate", "action": "lines",
            "schema_version": SCHEMA_VERSION, "image": a.image,
            "n_lines": len(lines), "lines": lines,
        }, ensure_ascii=False, indent=2))
        return EXIT_OK

    if sub == "list":
        items = MA.list_annotations(a.image, manual_dir=a.manual_dir)
        if not a.as_json:
            if not items:
                print(f"（{a.image} 还没有人工标注）")
            for it in items:
                print(f"{it['index']:>3}. [{it.get('attr') or '未标属性'}] "
                      f"{it.get('text')}   box={it.get('box')}  ts={it.get('ts')}")
            return EXIT_OK
        print(json.dumps({
            "ok": True, "exit": EXIT_OK, "subcommand": "annotate", "action": "list",
            "schema_version": SCHEMA_VERSION, "image": a.image,
            "n_items": len(items), "items": items,
            "file": str(MA.manual_path(a.image, a.manual_dir)),
        }, ensure_ascii=False, indent=2))
        return EXIT_OK

    if sub == "add":
        try:
            r = MA.add_annotation(
                image_name=a.image, box=a.box, text=a.text, attr=a.attr,
                profile=a.profile, note=a.note, gid=a.gid, xp=a.xp,
                line=a.line, lines=a.lines, pick=a.pick,
                structured_dir=a.structured_dir, outbox_dir=a.outbox,
                manual_dir=a.manual_dir)
        except MA.MissingError as e:      # 料不齐 → 3（去补材料）
            return _fail(EXIT_MISSING, str(e), "先补料（跑 ocr），再回来标注。", a.as_json)
        except MA.ValidateError as e:     # 参数不对 → 64（改参数）
            return _fail(EXIT_USAGE, str(e), "核对参数后重试。", a.as_json)
        rc = MA.annotate_exit_code(r)       # ★ 判定点在数据层，包装层不重判
        if not a.as_json:
            print(f"已追加 1 条 → {r.get('file')}")
            print(f"  {json.dumps(r.get('saved'), ensure_ascii=False)}")
            return rc
        print(json.dumps({
            "ok": r.get("ok"), "exit": rc,
            "subcommand": "annotate", "action": "add", "schema_version": SCHEMA_VERSION,
            "image": a.image, "saved": r.get("saved"), "file": r.get("file"),
            "error": r.get("error") or None,
        }, ensure_ascii=False, indent=2))
        return rc

    if sub == "remove":
        try:
            r = MA.remove_annotations(a.image, a.index, manual_dir=a.manual_dir)
        except MA.ValidateError as e:
            return _fail(EXIT_USAGE, str(e), "核对 --index 后重试。", a.as_json)
        rc = MA.annotate_exit_code(r)       # ★ 同 add：判定点在数据层
        if not a.as_json:
            print(f"已移除 {r.get('removed')} 条，余 {r.get('left')} 条 → {r.get('file')}")
            return rc
        print(json.dumps({
            "ok": r.get("ok"), "exit": rc,
            "subcommand": "annotate", "action": "remove", "schema_version": SCHEMA_VERSION,
            "image": a.image, "removed": r.get("removed"), "left": r.get("left"),
            "file": r.get("file"),
        }, ensure_ascii=False, indent=2))
        return rc

    return _fail(EXIT_USAGE, f"未知的 annotate 动作：{sub!r}",
                 "用 lines / list / add / remove 之一。", a.as_json)


# ============================================================
# 入口
# ============================================================
# 用法错误 2→64 的改写收口在 `cli_contract.Parser`（此前 5+ 个模块逐字抄写同一 override）。
# ⚠ stderr 文案随之统一为 `{prog}: error: …`（原「用法错误：」人读措辞退役 ——
#   契约是**退出码**，不是文案；agent 靠 64 自纠，人靠 usage 行）。
from cli_contract import Parser as _Parser                                 # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="chronicles",
        description="面向 agent 的统一命令面（纯包装；退出码 0/2/3/4/1，用法错误 64）",
        epilog="退出码：0 成功 · 2 面外拒答 · 3 缺料 · 4 校验不过 · 1 内部错误 · 64 用法错误",
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<subcommand>")

    p = sub.add_parser("triage", help="分诊：这批页像不像这个档案（面外拒答 = 退出码 2）")
    p.add_argument("--profile", required=True,
                   help="档案 id（prof_...）—— 锚词来源，判据由它决定")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--inbox", default=None, help="待分诊的页目录（扫图像后缀）")
    g.add_argument("--project", dest="project_id", default=None, help="按栏目取页（项目 id）")
    p.add_argument("--structured-dir", default=None, help="默认 data/structured")
    p.add_argument("--outbox", default=None, help="默认 <项目根>/outbox")
    p.add_argument("--allow-suspect", action="store_true",
                   help="显式放行面外页（退出码 0；清单仍照出 suspect）")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("facts", help="层 1：金标准 → facts（观测）")
    p.add_argument("--profile", required=True, help="profile id（prof_...）")
    p.add_argument("--out", default=None, help="输出目录（默认 data/plan）")
    p.add_argument("--gold", default=None,
                   help="金标准目录（默认 manual_annotations/）。"
                        "★ 只换目录；读**哪些行**由 --profile 决定（隔离）")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="结构化输出到 stdout（人读内容改道 stderr）")
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("gold",
                       help="金标准普查：我有哪些档案/项目、金标准做到哪一步（只读）")
    p.add_argument("--gold", default=None, help="金标准目录（默认 manual_annotations/）")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="结构化输出到 stdout（人读内容改道 stderr）")
    p.set_defaults(func=cmd_gold)

    p = sub.add_parser("plan", help="层 2：facts → 识别与标注方案（核心）")
    p.add_argument("--profile", required=True)
    p.add_argument("--out", default=None, help="输出目录（默认 data/plans）")
    p.add_argument("--facts-dir", default=None, help="默认 data/plan")
    p.add_argument("--contracts-dir", default=None, help="默认 data/layout_contracts")
    p.add_argument("--rules-dir", default=None, help="默认 rules_data")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("exec", help="层 3：plan → 产物")
    p.add_argument("--profile", required=True)
    p.add_argument("--stem", default="", help="只跑这一页（单页快捷方式）")
    p.add_argument("--pages", nargs="+", default=None,
                   help="跑哪些页：页名（可多个）或**目录**（跑目录里全部页）"
                        " —— 跑新材料的正确姿势")
    p.add_argument("--project", dest="project_id", default=None,
                   help="按栏目取页（项目 id）；与 `triage --project` 同源。"
                        "⚠ 不给页来源时跑的是**方案编译页**（金标准页），不是新页")
    p.add_argument("--plan-dir", default=None, help="默认 data/plans")
    p.add_argument("--out", default=None, help="产物**目录**（默认 data/results）")
    p.add_argument("--outbox", default=None,
                   help="页图目录；默认按 OUTBOX→INBOX 两处查（config.image_path_of）")
    p.add_argument("--structured-dir", default=None, help="默认 data/structured")
    p.add_argument("--no-verify", action="store_true", help="不跑内建自校验")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("eval-iou",
                       help="层 4：几何对评（**零 API**）：值对齐三口径 + 留一交叉验证"
                            " —— P5 第 ④ 道门「回验读数」的唯一来源")
    p.add_argument("--profile-id", required=True, help="档案 id（prof_...）")
    p.add_argument("--stem", default="",
                   help="只评这一页；**不给则做留一交叉验证**（每折留出一页学契约）")
    p.add_argument("--oracle", action="store_true",
                   help="★ 用金标准值当 records：**上界对照，不是读数**"
                        "（隔离 VLM 值质量，只看几何）")
    p.add_argument("--run-dir", default=None,
                   help="指定 VLM records 所在 run；缺省自动找含该页的最新 run")
    p.add_argument("--ann-dir", default=None, help="默认 manual_annotations")
    p.add_argument("--structured-dir", default=None, help="默认 data/structured")
    p.add_argument("--outbox", default=None, help="页图目录（默认 OUTBOX→INBOX 两处查）")
    p.add_argument("--divergence-dir", default=None, help="默认 data/divergence")
    p.add_argument("--contracts-dir", default=None, help="默认 data/layout_contracts")
    p.add_argument("--iou", type=float, default=None, help="IoU 命中阈值（默认 0.5）")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_eval_iou)

    p = sub.add_parser("draft",
                       help="层 3 产物 → 裁决草稿（人面入口：让 /annotate 看得到、改得动）")
    p.add_argument("--profile", required=True, help="档案 id（prof_...）—— 契约门面由它的 plan 提供")
    p.add_argument("--stem", default="", help="只桥这一页")
    p.add_argument("--pages", nargs="+", default=None,
                   help="桥哪些页：页名（可多个）或**目录**；不给则扫产物目录里本档案的全部产物")
    p.add_argument("--project", dest="project_id", default=None, help="按栏目取页（项目 id）")
    p.add_argument("--plan-dir", default=None, help="默认 data/plans")
    p.add_argument("--results-dir", default=None, help="产物目录（默认 data/results）")
    p.add_argument("--out", dest="out_dir", default=None,
                   help="草稿目录（默认 data/preannotations）")
    p.add_argument("--structured-dir", default=None, help="默认 data/structured")
    p.add_argument("--outbox", default=None,
                   help="页图目录；默认按 OUTBOX→INBOX 两处查（config.image_path_of）")
    p.add_argument("--overwrite", action="store_true",
                   help="覆盖**内容不同**的既有草稿（默认拒绝，保护界面那条通道写下的草稿）；"
                        "被覆盖的原文会自动归档到 `_archive/draft_superseded/`")
    p.add_argument("--archive-dir", default=None,
                   help="被覆盖草稿的归档目录（默认由 --out 派生到项目 `_archive/`）")
    p.add_argument("--dry-run", action="store_true", help="只对账、不落盘")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("sample",
                       help="跑批产物随机抽 n 页交人复核"
                            "（全量通过判定前的抽样口；seed 可复现）")
    p.add_argument("--n", type=int, default=3,
                   help="抽几页（默认 3；池小于 n 时全给）")
    p.add_argument("--seed", type=int, default=None,
                   help="随机种子（缺省现场生成并回报；同 seed 结果可复现）")
    p.add_argument("--results-dir", default=None, help="产物目录（默认 data/results）")
    p.add_argument("--gold-dir", default=None,
                   help="金标准目录（默认 manual_annotations；命中页被排除）")
    p.add_argument("--outbox", default=None,
                   help="页图目录；默认按 OUTBOX→INBOX 两处查（config.image_path_of）")
    p.add_argument("--profile", default=None,
                   help="可选：档案 id（prof_...）—— 抽完后**回查**材料画像 QA flags"
                        "（信封加 picked_qa + 人读一行优先阅读提示）。"
                        "★ 纯呈现：随机抽选一字不动（flags 进选择=分层，明文禁止）")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("adjudicate",
                       help="草稿裁决：list / accept / reject / reset / batch / validation"
                            "（技能包的裁决入口；批量走数据层安全门，"
                            "放行依据 = 档案级回验读数）")
    asub = p.add_subparsers(dest="adj_cmd", required=True, metavar="<action>")

    def _adj_common(q):
        q.add_argument("--stem", required=True, help="页（stem 或 image_name，如 0001）")
        q.add_argument("--image", default="", help="图名（默认 <stem>.png）")
        q.add_argument("--drafts-dir", default=None, help="默认 data/preannotations")
        q.add_argument("--status-path", default=None,
                       help="默认 data/adjudication/status.json")
        q.add_argument("--manual-dir", default=None, help="默认 manual_annotations/")
        q.add_argument("--archive-dir", default=None, help="默认项目 `_archive/`")
        q.add_argument("--structured-dir", default=None, help="默认 data/structured")
        q.add_argument("--drift-dir", default=None, help="漂移状态目录（默认 data/drift）")
        q.add_argument("--image-dir", default=None,
                       help="页图目录；不给则按 OUTBOX→INBOX 查（`config.image_path_of`）"
                            "—— 无原图的页不允许批量采纳（P4）")
        q.add_argument("--validation-dir", default=None,
                       help="回验读数目录（默认 data/validation）")
        q.add_argument("--json", action="store_true", dest="as_json")
        q.set_defaults(func=cmd_adjudicate)

    q = asub.add_parser("list",
                        help="本页草稿 + 裁决状态 + 批量准入（与界面**同一个取数口**）")
    _adj_common(q)

    q = asub.add_parser("accept",
                        help="采纳一条（可带 --text/--box/--attr ⇒ 即界面上的「改后采纳」）")
    _adj_common(q)
    q.add_argument("--idx", required=True, type=int, help="草稿序号（见 list）")
    q.add_argument("--text", default=None, help="覆盖文本（不给则用草稿的）")
    q.add_argument("--box", default=None,
                   help="覆盖框 `x1,y1,x2,y2`（像素；给了即按单段落库）")
    q.add_argument("--attr", default=None, help="覆盖属性名")

    q = asub.add_parser("reject", help="忽略一条（只记状态，草稿原样留痕）")
    _adj_common(q)
    q.add_argument("--idx", required=True, type=int)

    q = asub.add_parser("reset",
                        help="撤销一条已采纳（按 ai_ref 精确移除行 + 清状态；幂等）")
    _adj_common(q)
    q.add_argument("--idx", required=True, type=int)

    q = asub.add_parser("batch",
                        help="批量裁决（★ 安全门在数据层；本命令**不开口子**）")
    _adj_common(q)
    g = q.add_mutually_exclusive_group()
    g.add_argument("--indices", default="",
                   help="人的判断：显式序号如 `1,3,7`（人已逐条点过 ⇒ 免几何可信性检查）")
    g.add_argument("--confidences", default="",
                   help="机器的判断：按置信层如 `high,medium`（须过安全门，默认 high）")
    q.add_argument("--action", default="accept", choices=["accept", "reject"],
                   help="批量动作（默认 accept）")

    # ★ 本子命令**不用** `_adj_common`：粒度是**档案**，与页无关（见 `cmd_adjudicate`）。
    q = asub.add_parser("validation",
                        help="档案级回验读数：登记 / 查询"
                             "（★ 批量采纳的唯一放行依据；粒度 = **档案**，不是页不是期次）")
    q.add_argument("--profile-id", default="",
                   help="档案 id（prof_…）—— 读数是**档案级**的")
    q.add_argument("--verdict", default=None,
                   help="pass / fail（**没有第三种**：拿不准就是 fail）；"
                        "不给 = 只查询现有读数，**不写盘**")
    q.add_argument("--metric", default=None,
                   help="度量名（默认 manual）。★ 可实算的只有 "
                        "`value_aligned_iou50`（分母 = **有属性名**的金标准条目，"
                        "对称分母；由 `chronicles eval-iou` 产出）；`gold_iou50` 是历史名、"
                        "**无实现**；`manual` 表示人工判断。"
                        "★ 换口径会改变历史读数的含义 ⇒ 必须**同批**重登记")
    q.add_argument("--value", type=float, default=None, help="读数，如 0.62")
    q.add_argument("--n-boxes", type=int, default=None, help="回验覆盖的框数")
    q.add_argument("--pages", default="", help="参与回验的金标准页（逗号分隔）")
    q.add_argument("--note", default="", help="说明（人读；供以后追「当初为什么这么判」）")
    q.add_argument("--validation-dir", default=None, help="默认 data/validation")
    q.add_argument("--archive-dir", default=None,
                   help="旧记录的归档目录（默认项目 `_archive/`）；覆盖前必归档、"
                        "归档失败即拒绝写入")
    q.add_argument("--json", action="store_true", dest="as_json")
    q.set_defaults(func=cmd_adjudicate)

    p = sub.add_parser("next",
                       help="下一步去哪做：看每页卡在哪一步，把界面入口给你"
                            "（本命令只探测；起服务用 `chronicles ui --serve`）")
    p.add_argument("--profile", default="", help="档案 id（用于判断该不该重学一次）")
    p.add_argument("--project", dest="project_id", default=None, help="只看这个栏目")
    p.add_argument("--stem", default="", help="只看这一页")
    p.add_argument("--limit", type=int, default=20, help="最多给几个地址（默认 20）")
    p.add_argument("--gold", default=None, help="金标准目录（默认 manual_annotations/）")
    p.add_argument("--drafts-dir", default=None, help="默认 data/preannotations")
    p.add_argument("--structured-dir", default=None, help="默认 data/structured")
    p.add_argument("--outbox", default=None, help="默认 <项目根>/outbox")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("ui",
                       help="界面通道：给标注页地址；`--serve` 起服务"
                            "（★ 2026-09-16 新裁定：可在管线必要节点起服务）")
    import ui_cli as _UI
    # ★ 参数定义**只有一份**（`ui_cli.add_arguments`）—— 不在两处各写一遍（§60）
    _UI.add_arguments(p)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("verify", help="自校验（seam.validate）")
    p.add_argument("--result", required=True, help="result.json 路径")
    p.add_argument("--plan", default="", help="plan 路径（给了才做对位判据）")
    p.add_argument("--no-source", action="store_true", help="不解析真实输入流（较弱）")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("map", help="方案结构图（IR / SVG）")
    p.add_argument("--profile", default="", help="档案 id（缺省取最新 plan）")
    p.add_argument("--svg", default="", help="SVG 落盘路径")
    p.add_argument("--ir", default="", help="IR 落盘路径（--json 时另见 stdout）")
    p.add_argument("--plan", default="", help="画指定的 plan 文件")
    p.add_argument("--gold", default="", help="金标准目录")
    p.add_argument("--gold-profile", default="", help="金标准按哪套算")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("ocr", help="OCR：图像 → 结构化 JSON（批量档；3 缺料 / 1 有页失败）")
    p.add_argument("--images", nargs="+", required=True,
                   help="图像目录（扫顶层）或图像文件，可给多个")
    p.add_argument("--out", default=None, help="结构化落盘目录（默认 data/structured）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 张（0 = 全部）")
    p.add_argument("--prefetch", type=int, default=None,
                   help="覆盖批量提交张数（0 = 逐张档；默认 config.OCR_BATCH_PREFETCH）")
    p.add_argument("--no-prefetch", action="store_true", help="等价于 --prefetch 0")
    p.add_argument("--parallel", type=int, default=None,
                   help="覆盖并发数（默认 config.OCR_BATCH_PARALLEL）")
    p.add_argument("--dry-run", action="store_true", help="只列将跑哪些图，不调用 API")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("import", help="图片/PDF 导入：外部材料 → inbox/（保留原名 + 像素去重）")
    p.add_argument("--images", nargs="+", required=True,
                   help="图像或 PDF 的目录（可递归）或文件，可给多个")
    p.add_argument("--to", choices=["inbox", "outbox"], default="inbox",
                   help="落位目录（默认 inbox，与剪贴板通道一致）")
    p.add_argument("--limit", type=int, default=0, help="只导前 N 张（0 = 全部）")
    p.add_argument("--pdf-dpi", type=int, default=None,
                   help="PDF 渲染 dpi（默认 200，钳制 72..400，与界面批量导入同口径）。"
                        "★ OCR 质量的第一决定因素 —— 竖排小字材料宜用 300+")
    p.add_argument("--no-recursive", action="store_true", help="目录不递归")
    p.add_argument("--dry-run", action="store_true", help="只列将导入什么，不复制")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("profile",
                       help="档案：new / list / show / add-attr / scan（材料画像）/ "
                            "**learn**（学规则）/ rules（规则说明书）/ confirm（确认登记）/ "
                            "override（人工覆盖层：pin 属性序 / 强制必填）")
    psub = p.add_subparsers(dest="profile_cmd", required=True, metavar="<action>")
    import profile_cli as _PC
    # ★ 参数定义**只有一份**（`profile_cli.add_subcommands`）—— 不在两处各写一遍（§60）
    _PC.add_subcommands(psub, func=cmd_profile)

    p = sub.add_parser("project",
                       help="专项栏目：new / list / show / assign / images / template")
    jsub = p.add_subparsers(dest="project_cmd", required=True, metavar="<action>")
    import project_cli as _PJ
    _PJ.add_subcommands(jsub, func=cmd_project)

    p = sub.add_parser("annotate", help="人工标注（金标准页）：lines / list / add / remove")
    asub = p.add_subparsers(dest="annotate_cmd", required=True, metavar="<action>")

    q = asub.add_parser("lines", help="列出该页 OCR 行（编号+文本+框）—— agent 提框的材料")
    q.add_argument("--image", required=True, help="图名（如 0001.png）")
    q.add_argument("--structured-dir", default=None, help="默认 data/structured")
    q.add_argument("--outbox", default=None, help="默认 <项目根>/outbox")
    q.add_argument("--json", action="store_true", dest="as_json")
    q.set_defaults(func=cmd_annotate)

    q = asub.add_parser("list", help="列出该页已有的人工标注")
    q.add_argument("--image", required=True)
    q.add_argument("--manual-dir", default=None,
                   help="标注目录（默认 manual_annotations/；测试隔离用）")
    q.add_argument("--json", action="store_true", dest="as_json")
    q.set_defaults(func=cmd_annotate)

    q = asub.add_parser("add",
                        help="追加一条人工标注（框来自 --line / --lines 或 --box）")
    q.add_argument("--image", required=True)
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--line", type=int, default=None,
                   help="取该页第 N 行 OCR 行（1 起）的框与文本")
    g.add_argument("--lines", default=None,
                   help="取这几行的**并集框**：'1,2' 或 '1-3'"
                        "（竖排古籍一条记录常被切成几格，单行取不全）")
    g.add_argument("--box", type=_parse_box, default=None,
                   help="显式给框：x1,y1,x2,y2（界面画框的等价物）")
    q.add_argument("--pick", default=None,
                   help="**与 --line 合用**：只取该行里的这一个子串，按字数比例切出框"
                        "（终端里给不出像素坐标、但给得出文本时的正解）")
    q.add_argument("--text", default="",
                   help="文本（--line/--lines 时缺省取 OCR 行文本，多行按行序拼接；"
                        "--pick 时缺省取 pick 本身）")
    q.add_argument("--attr", default="", help="属性名（来自档案属性集）")
    q.add_argument("--profile", default="", help="档案 id（prof_*）")
    q.add_argument("--note", default="")
    q.add_argument("--gid", default="", help="跨列续框组 id")
    q.add_argument("--xp", choices=["", "prev", "next"], default="")
    q.add_argument("--structured-dir", default=None, help="默认 data/structured")
    q.add_argument("--outbox", default=None, help="默认 <项目根>/outbox")
    q.add_argument("--manual-dir", default=None,
                   help="标注目录（默认 manual_annotations/；测试隔离用）")
    q.add_argument("--json", action="store_true", dest="as_json")
    q.set_defaults(func=cmd_annotate)

    q = asub.add_parser("remove", help="按序号移除标注（1 起）")
    q.add_argument("--image", required=True)
    q.add_argument("--index", type=int, nargs="+", required=True, help="要移除的序号（1 起）")
    q.add_argument("--manual-dir", default=None,
                   help="标注目录（默认 manual_annotations/；测试隔离用）")
    q.add_argument("--json", action="store_true", dest="as_json")
    q.set_defaults(func=cmd_annotate)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
