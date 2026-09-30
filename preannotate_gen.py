# -*- coding: utf-8 -*-
"""数据飞轮 · 半自动标注流水线（2026-09-11）。

**它回答的问题**（用户 2026-09-11 对飞轮的定义）：

    用户给**少量**标注好的例题，系统从中提取规则，
    用规则去读**其余绝大多数**没有标注的图；
    系统怀疑读错的地方进待核工作台由用户裁决，
    裁决结果再作为调整规则的材料 → 更高精度地自动标注。

**这是那条飞轮的执行段**（第二段到第四段）：

    阶段 1  少量金标准 ──rule_learn──▶ 规则（rules_data/learned_<pid>.json）
    阶段 2  规则 ──注入 split_records 的提示──▶ 参与识别（事前，不是事后校验）
    阶段 3  全样本 ──本模块──▶ 预标注草稿（data/preannotations/<stem>.ai.jsonl）
    阶段 4  规则校验不过 / 锚定不实 ──▶ 条目标 low + evidence.rule 说明（= 疑错）
            → 待核工作台（/review）

**与既有模块的关系**：几何锚定用 `preannotate.anchor_page`、拆分用
`split_records.split_page`、结构化落盘用 `structured_writer.StructuredWriter`、
规则校验用 `rule_engine` —— 本模块**只做编排**，不重造任何一环。

纪律：
  - **绝不丢字**：每页对"原始块内容 vs 解析行文本"做字符多重集核对，真丢字即报错停页
  - **落盘先备份**：覆盖 `data/structured/<stem>.json` 前先复制进 `_archive/`（可回退）
  - **逐页隔离**：单页失败不中断整批（批量跑到第 4 页整体崩过一次，见 既有记录）
  - **零静默**：每页产出记录/条目/锚定率/疑错数，全部回传
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("preannotate_gen")

_BASE = (Path(__import__("sys").executable).parent.resolve()
         if getattr(__import__("sys"), "frozen", False)
         else Path(__file__).parent.resolve())

DEFAULT_STRUCTURED_DIR = _BASE / "data" / "structured"
DEFAULT_DRAFTS_DIR = _BASE / "data" / "preannotations"
DEFAULT_OUTBOX = _BASE / "outbox"
DEFAULT_ARCHIVE = _BASE / "_archive"

_TAG_RE = re.compile(r"<[^>]+>")


# ============================================
# 文本完整性（红线：绝不丢字）——单一实现入口
# ============================================
def raw_blocks(raw_jsonl: str) -> List[tuple]:
    """原始 JSONL → [(block_content, bbox, label)]，不做任何加工。

    **必须拿"原始块"比"解析行"**：早前用"含 HTML 标签的原始块"直接比
    "去标签的解析文本"，把标签算成内容，误报过 90 字丢字（口径错误，
    已在 既有记录）。故此处连同 `plain_text` 一起收敛为单一实现。
    """
    out: List[tuple] = []
    for ln in (raw_jsonl or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        for page in d.get("result", {}).get("layoutParsingResults", []):
            for b in page.get("prunedResult", {}).get("parsing_res_list", []):
                out.append((b.get("block_content") or "",
                            b.get("block_bbox"),
                            b.get("block_label") or ""))
    return out


def plain_text(s: str) -> str:
    """去 HTML 标签 + 去所有空白（换行不算"字"，比对前必须归一）。"""
    return re.sub(r"\s+", "", _TAG_RE.sub("", s or ""))


def text_loss_check(blocks: List[tuple], lines: List[dict]) -> dict:
    """解析前后零丢字核对 → `{raw, out, delta, same_order, same_multiset}`。

    `same_multiset=True` 而 `same_order=False` → **纯重排**（内容未丢，
    只是阅读序变了，属"读法"问题）；`same_multiset=False` → **真丢字**。
    """
    src = plain_text("".join(t for t, _b, _l in blocks))
    out = plain_text("".join(l.get("text") or "" for l in lines))
    return {"raw": len(src), "out": len(out), "delta": len(out) - len(src),
            "same_order": src == out, "same_multiset": sorted(src) == sorted(out)}


# ============================================
# 规则校验 → 疑错（飞轮第四段的入口）
# ============================================
def rule_flags(profile: dict, records: List[dict]) -> Dict[int, dict]:
    """逐记录跑 `rule_engine` 校验 → `{记录序号: {suspects, fixes}}`。

    **只对"有值"的属性判定**：空值谈不上对错，若一并算 SUSPECT，
    会把整页条目无差别打成 low（规则层反而失去分辨力）。
    """
    try:
        import rule_engine as RE
    except Exception as e:                               # pragma: no cover
        log.warning("[preannotate_gen] rule_engine 不可用: %s", e)
        return {}
    try:
        schema = RE.build_schema_from_profile(profile)
    except Exception as e:                               # pragma: no cover
        log.warning("[preannotate_gen] 规则schema构建失败: %s", e)
        return {}
    flags: Dict[int, dict] = {}
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        suspects: Dict[str, dict] = {}
        fixes: Dict[str, dict] = {}
        try:
            results = schema.validate_record(rec)
        except Exception as e:                           # pragma: no cover
            log.warning("[preannotate_gen] 记录 %d 校验异常: %s", i, e)
            continue
        for name, rr in results.items():
            if not str(rec.get(name) or "").strip():
                continue                                 # 空值不判
            if rr.status == RE.STATUS_SUSPECT:
                suspects[name] = {"rule": rr.rule, "reason": rr.reason}
            elif rr.status == RE.STATUS_FIXED:
                fixes[name] = {"rule": rr.rule, "reason": rr.reason}
        if suspects or fixes:
            flags[i] = {"suspects": suspects, "fixes": fixes}
    return flags


def apply_flags(entries: List[dict], flags: Dict[int, dict]) -> int:
    """把疑错挂到草稿条目上：命中 SUSPECT → 降为 low + `evidence.rule`。

    挂载点是 `evidence.record_index`（`anchor_page` 已写入，无需改锚定层）。
    返回被打上疑错的条目数。
    """
    n = 0
    for e in entries:
        ri = (e.get("evidence") or {}).get("record_index")
        fl = flags.get(ri)
        if not fl:
            continue
        a = str(e.get("attr") or "")
        if a in fl.get("suspects", {}):
            s = fl["suspects"][a]
            e["confidence"] = "low"
            e["evidence"]["rule"] = {"status": "SUSPECT", **s}
            n += 1
        elif a in fl.get("fixes", {}):
            e["evidence"]["rule"] = {"status": "FIXED", **fl["fixes"][a]}
    return n


def recompute_stats(entries: List[dict]) -> dict:
    """按**改动后**的条目重算置信/定位分布（锚定层的统计是改动前的）。"""
    conf = Counter(e.get("confidence") for e in entries)
    loc = Counter((e.get("evidence") or {}).get("locate") for e in entries)
    ruled = sum(1 for e in entries if (e.get("evidence") or {}).get("rule"))
    susp = sum(1 for e in entries
               if ((e.get("evidence") or {}).get("rule") or {}).get("status") == "SUSPECT")
    return {"conf": dict(conf), "locate": dict(loc),
            "n_flagged": ruled, "n_suspect": susp}


def human_error(e: BaseException) -> str:
    """把 provider 原始异常翻译成**可行动**的中文提示。

    `HTTP 402 Insufficient Balance` 这种字符串对用户毫无指导性——它既没说
    是哪个环节，也没说该做什么。**资源类错误必须与代码缺陷区分开**：
    前者用户充值/换模型即可，后者才需要报 bug。

    （2026-09-11 批处理实测：跑至第 20 页余额耗尽，剩下 6 页全部
    `split_failed: HTTP 402 ...`，若不做翻译，界面与日志都无法自证原因。）
    """
    msg = str(e)
    low = msg.lower()
    if "402" in msg or "insufficient balance" in low or "insufficient_quota" in low:
        return ("拆分模型余额/额度不足（HTTP 402）——请到模型服务商充值，"
                "或在 数据/LLM 配置 里换一个可用模型后重跑。"
                f"（原始信息：{msg[:160]}）")
    if "401" in msg or "invalid_api_key" in low or "unauthorized" in low:
        return f"拆分模型鉴权失败（HTTP 401）——请检查 API Key 配置。（原始信息：{msg[:160]}）"
    if "429" in msg or "rate limit" in low:
        return f"拆分模型限流（HTTP 429）——稍后重跑即可，已产出的草稿不会重做。（原始信息：{msg[:160]}）"
    if "timeout" in low or "timed out" in low:
        return f"拆分调用超时——稍后重跑即可（该页会重新尝试）。（原始信息：{msg[:160]}）"
    return msg


# ============================================
# 依赖构造（与生产同源）
# ============================================
def build_engine():
    """线上 OCR 引擎（与生产同源取配置：ocr_config 单一真相，2026-09-30 与壳解耦）。"""
    from ocr_config import load_baidu_config
    from ocr_backend import get_backend
    cfg = load_baidu_config()
    return get_backend("paddleocr_vl",
                       token=cfg.get("paddleocr_vl_token", ""),
                       job_url=cfg.get("paddleocr_vl_job_url", ""))


def build_split_client():
    """拆分用文本 LLM（读生产 llm 配置）。"""
    import llm_client
    return llm_client.get_llm_client(
        llm_client.load_config(_BASE / "data" / "llm_config.json"))


def llm_prices():
    """拆分用 LLM 的参考单价（元 / 百万 tokens）——**只为成本取证显示**。

    不参与任何识别决策；配置里没写就退回 `split_l1` 的内置默认。
    单独成函数是为了让"钱花在哪"这件事在界面上有据可查（用户对线上花费敏感）。
    """
    try:
        import llm_client
        cfg = llm_client.load_config(_BASE / "data" / "llm_config.json")
    except Exception:                                      # pragma: no cover
        cfg = {}
    import split_l1 as L1
    return L1.prices_from_config(cfg)


# ============================================
# 单页
# ============================================
def save_l0_meta(stem: str, meta: dict, drafts_dir: Path) -> None:
    """把 L0 的**页级自检**（置信 / 骨架填充率 / 未覆盖字符）留一份旁证。

    落 `<stem>.l0.json`，**不塞进草稿 jsonl**：草稿层是「条目」的数组，
    `adjudicate` 把它当唯一事实源只读消费；混入页级元信息会污染这个契约。
    旁证独立、可缺失、可重建，界面拿它只为**提示**（"这页整体读得不好，
    请优先复核"），不参与任何判定。`list_draft_stems` 只匹配 `*.ai.jsonl`，
    故旁证不会干扰草稿枚举。

    写失败**不影响主流程**（旁证是增益，不是必需）。
    """
    try:
        import data_io
        d = Path(drafts_dir)
        d.mkdir(parents=True, exist_ok=True)
        data_io.atomic_write_json(d / f"{data_io.safe_name(stem)}.l0.json", meta)
    except Exception as e:                                 # pragma: no cover
        log.warning("[preannotate_gen] L0 旁证写入失败 %s: %s", stem, e)


def load_l0_meta(stem: str, drafts_dir: Path) -> Optional[dict]:
    """读 L0 旁证（无则 None，口径与 `save_l0_meta` 严格对称）。"""
    try:
        import data_io
        p = Path(drafts_dir) / f"{data_io.safe_name(stem)}.l0.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:                                          # pragma: no cover
        return None


def write_structured(stem: str, result: dict, structured_dir: Path,
                     backup_dir: Optional[Path], engine) -> bool:
    """落 `data/structured/<stem>.json`（覆盖前先备份，可回退）。

    ✅ 2026-09-14（技能包第 0 步补完）：由 `_write_structured` **提为公开**
    —— OCR 命令面（`ocr_cli.py`）要复用它，而 §60 纪律要求**单一实现**，
    故提公开而非在别处复制这十行。旧名保留为别名（既有调用点零变化）。
    """
    import structured_writer as SW
    p = structured_dir / f"{stem}.json"
    if p.exists() and backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(p, backup_dir / p.name)
        except OSError as e:
            log.warning("[preannotate_gen] 备份失败 %s: %s", p.name, e)
    w = SW.StructuredWriter(structured_dir)
    return bool(w.save(
        image_name=f"{stem}.png",
        ocr_backend=getattr(engine, "name", "paddleocr_vl"),
        ocr_model=getattr(engine, "model", ""),
        columns=result.get("columns") or [],
        lines=result.get("lines") or [],
        quality="", xlsx_id_start=1, quality_map={},
        blocks=result.get("blocks"),
        block_order=result.get("block_order") or ""))


# 向后兼容别名（本文件内及既有外部调用点均不必改）
_write_structured = write_structured


def one_page(stem: str, ctx: dict) -> dict:
    """单页全链路 → 统计记录。**异常不外抛**（逐页隔离，落 error 字段）。"""
    img = Path(ctx["outbox_dir"]) / f"{stem}.png"
    rec: dict = {"stem": stem}
    if not img.exists():
        rec["error"] = "image_missing"
        return rec
    PA, sp = ctx["PA"], ctx["sp"]
    profile, rules = ctx["profile"], ctx["rules"]
    sdir, ddir = ctx["sdir"], ctx["ddir"]

    # ---- 阶段 1：OCR（线上，block 模式 → 原生几何）----
    # `skip_ocr`：复用已有 `data/structured/<stem>.json`。L0 只需要「行 + 几何」，
    # 不需要重新 OCR —— 批处理实测 26 页重 OCR 要约 40 分钟，复用则秒级。
    lines: List[dict] = []
    result = None
    if ctx.get("skip_ocr") and (sdir / f"{stem}.json").exists():
        lines = PA.page_lines(stem, sdir, ctx["outbox_dir"])
        rec["ocr"] = "reused"
        if not lines:
            rec["error"] = "structured_lines_empty"
            return rec
    else:
        t0 = time.time()
        last: Optional[Exception] = None
        for _ in range(max(1, ctx["retries_ocr"])):
            try:
                result = ctx["engine"].recognize(str(img))
                break
            except Exception as e:
                last = e
        if result is None:
            rec["error"] = f"ocr_failed: {human_error(last)}"
            return rec
        rec["t_ocr"] = round(time.time() - t0, 1)

        lines = result.get("lines") or []
        if not lines:
            rec["error"] = "ocr_empty"
            return rec

        # ---- 阶段 1b：零丢字核对（红线）----
        lc = text_loss_check(raw_blocks(result.get("raw_jsonl") or ""), lines)
        rec["loss"] = lc
        if not lc["same_multiset"]:
            rec["error"] = f"text_loss: {lc['delta']}"
            return rec

        # ---- 阶段 1c：结构化落盘（原生几何）----
        try:
            ok = _write_structured(stem, result, sdir, ctx.get("backup_dir"),
                                   ctx["engine"])
        except Exception as e:
            rec["error"] = f"structured_failed: {e}"
            return rec
        if not ok:
            rec["error"] = "structured_save_failed"
            return rec
        rec["ocr"] = "fresh"
    rec.update({"lines": len(lines),
                "chars": sum(len(l.get("text") or "") for l in lines)})

    # ---- 阶段 2：拆分 ----
    # **成本从低到高三级递进**，每页只走到"够用"的那一级为止：
    #   rule → **只用 L0**（零 LLM 调用）；L0 无产出即该页失败
    #   l1   → L0 + **L1 指针补全**（模型只报行号；不调用 L2 兜底）
    #   auto → L0 优先，整页读不出才兜底 L2（默认档；**不跑 L1**）
    #   llm  → 只用 L2（旧行为，逐字不变）
    mode = str(ctx.get("split_mode") or "auto")
    records: List[dict] = []
    details: List[dict] = []
    r0: dict = {}
    l0_failed = False        # L0「读不出」（零产出 / 整页兜底一条）——兜底链的触发条件
    out: Optional[dict] = None
    t0 = time.time()
    if mode in ("rule", "l1", "auto"):
        try:
            import rule_split as RS
            r0 = RS.split_page(stem, ctx.get("learned") or {}, profile,
                               structured_dir=sdir, outbox_dir=ctx["outbox_dir"])
        except Exception as e:                       # L0 异常不得中断整批
            log.warning("[preannotate_gen] %s L0 切分失败: %s", stem, e)
            r0 = {"records": [], "details": [], "units": [],
                  "parse_meta": {"parse_ok": False, "reason": f"l0_error: {e}"}}
        rec["l0"] = r0["parse_meta"]
        save_l0_meta(stem, r0["parse_meta"], ddir)      # 页级自检旁证（界面提示用）
        records = r0.get("records") or []
        details = r0.get("details") or []
        # 「整页兜底一条」是 L0 **自己承认"整页读不出"**（未找到记录起点行）——
        # 它是低置信占位，**不是成功产出**：不能让它把 `auto` 的兜底链整条短路。
        # （2026-09-11 实测踩到：`样例材料_0004` L0 兜底 1 条 → `auto` 既不补 L1
        #   也不兜底 L2，页面就停在一条畸形记录上。修后该页 L1 重划出 4 条记录。）
        l0_failed = (not records) or bool(
            (r0.get("parse_meta") or {}).get("records_fallback"))
        if records:
            rec["splitter"] = "rule_l0"
            rec["t_split"] = round(time.time() - t0, 1)
        elif mode == "rule":
            rec["error"] = f"l0_empty: {r0['parse_meta'].get('reason')}"
            return rec

    # ---- 阶段 2b：L1 指针补全（模型只报行号，值一律由本地从原文切）----
    # 触发条件只有两个，都**有据可依**：
    #   ① 用户显式选 `l1` 档 → 整批走 L1；
    #   ② `auto` 且 **L0 读不出**（零产出 / 整页兜底一条）→ 先试 L1 再谈 L2：
    #      ¥0.0036/页 vs L2 ¥0.0074/页，且 L1 **不生成正文**（幻觉结构上不可能）。
    # 反面证据同样要记住：**L0 已经切对的页上 L1 零增益**（例题三页逐位打平，
    # → 所以 `auto` **绝不在 L0 成功的页上白花这笔钱**。
    if mode == "l1" or (mode == "auto" and l0_failed):
        try:
            import split_l1 as L1
            l0_units = r0.get("units") or []
            if l0_units:
                t1 = time.time()
                # 读不出时的两种情形都**必须重划**，补空救不了
                force_full = l0_failed
                pi, po = (ctx.get("prices") or (None, None))
                r1 = L1.l1_page(ctx["client_split"], lines=l0_units,
                                records=[] if force_full else records,
                                details=[] if force_full else details,
                                profile=profile, learned=ctx.get("learned"),
                                force_full=force_full, price_in=pi, price_out=po)
                m1 = r1.get("parse_meta") or {}
                ap = m1.get("apply") or {}
                rec["l1"] = {"mode": m1.get("mode"), "reason": m1.get("reason", ""),
                             "parse_ok": m1.get("parse_ok"),
                             "n_filled": ap.get("n_filled", 0),
                             "n_rejected": ap.get("n_rejected", 0),
                             "n_gap": m1.get("n_gap"),
                             "reject_reasons": ap.get("reject_reasons")}
                rec["l1_t"] = round(time.time() - t1, 1)
                rec["l1_usage"] = r1.get("usage")
                rec["l1_cost"] = r1.get("cost_cny")
                new_records = r1.get("records") or []
                if new_records:
                    records = new_records
                    n_filled = rec["l1"].get("n_filled") or 0
                    rec["splitter"] = ("rule_l1" if force_full else
                                       ("rule_l0+l1" if n_filled else "rule_l0"))
                # L1 读不出时**保住 L0 的产出**（宁可有低置信草稿，也不把页面清空）
        except Exception as e:                       # L1 异常同样不中断整批
            log.warning("[preannotate_gen] %s L1 补全失败: %s", stem, e)
            rec["l1"] = {"reason": f"l1_error: {e}"}
        # **`auto` 档的统一收口**：L1 没接住（异常 / 无行可指 / 读不出记录）时，
        # 丢掉 L0 那条**畸形兜底**，让下面的 L2 兜底链接手。
        # 判断依据：`auto` 下 `l0_failed` 必然 `force_full=True`，接住就会写成 `rule_l1`。
        # 例外只在 `l1` 档 —— 它没有 L2 可接，故保留低置信草稿不清空。
        if mode == "auto" and l0_failed and rec.get("splitter") != "rule_l1":
            records, details = [], []
        # L1 口径**并入 L0 旁证**（同一个 `<stem>.l0.json`）：界面已有读它的通路，
        # 不新开文件、不新开接口，也不动草稿层契约（旁证是增益，可缺失）。
        try:
            side = load_l0_meta(stem, ddir) or {}
            side["l1"] = rec.get("l1") or {}
            side["l1_cost_cny"] = rec.get("l1_cost")
            side["l1_t"] = rec.get("l1_t")
            side["splitter"] = rec.get("splitter")
            save_l0_meta(stem, side, ddir)
        except Exception as e:                       # pragma: no cover
            log.warning("[preannotate_gen] %s L1 旁证合并失败: %s", stem, e)

    if not records and mode in ("llm", "auto"):
        page_text = "\n".join(l.get("text") or "" for l in lines)
        t0 = time.time()
        # 空结果重试一次：模型偶发返回空 records（2026-09-11 批处理实测，同页离线复现
        # 却成功 → 属瞬时不确定性，不是确定失败）。重试成本 ~1 分钟，远低于漏一页。
        for attempt in (1, 2):
            try:
                out = sp.split_page(ctx["client_split"], page_text, profile,
                                    rules=(rules or None))
            except Exception as e:
                rec["error"] = f"split_failed: {human_error(e)}"
                return rec
            records = out.get("records") or []
            if records:
                break
            log.warning("[preannotate_gen] %s 拆分空结果（第 %d 次）→ 重试", stem, attempt)
        rec.update({"t_split": round(time.time() - t0, 1),
                    "splitter": "llm",
                    "parse_ok": (out or {}).get("parse_meta", {}).get("parse_ok"),
                    "max_tokens_used": (out or {}).get("max_tokens_used")})
    if not records:
        # `l1` 档没有 L2 兜底（用户明确选了省资源档）→ 错误信息里说清"下一步该选什么"
        rec["error"] = ("split_empty" if mode != "l1"
                        else "l1_empty: 规则与指针补全都未切出记录"
                             "（该页需人工切分，或改用「L0 优先」档兜底线上模型）")
        return rec
    rec["records"] = len(records)

    # ---- 阶段 3：规则校验（疑错）→ 锚定 → 草稿 ----
    flags = rule_flags(profile, records)
    try:
        r = PA.preannotate_stem(stem, records, ctx["profile_id"],
                                structured_dir=sdir, outbox_dir=ctx["outbox_dir"])
    except Exception as e:
        rec["error"] = f"anchor_failed: {e}"
        return rec
    if r.get("error"):
        rec["error"] = f"anchor_error: {r['error']}"
        return rec
    entries, st = r.get("entries") or [], r.get("stats") or {}
    n_susp = apply_flags(entries, flags)
    try:
        PA.save_drafts(entries, stem, drafts_dir=ddir)
    except Exception as e:
        rec["error"] = f"draft_save_failed: {e}"
        return rec
    rs = recompute_stats(entries)
    rec.update({"entries": len(entries), "stats": st, **rs,
                "records_flagged": len(flags)})
    return rec


# ============================================
# 批量入口
# ============================================
def generate(stems: List[str], profile_id: str, *,
             engine=None, split_client=None,
             structured_dir=None, drafts_dir=None, outbox_dir=None,
             backup_dir=None, use_rules: bool = True,
             split_mode: str = "auto", skip_ocr: bool = False,
             skip_existing: bool = False, retries_ocr: int = 2,
             progress: Optional[Callable[[int, int, str, dict], None]] = None,
             pages_dir: Optional[Path] = None) -> dict:
    """对一批页跑「规则驱动 → 自动标注 → 疑错」全链路。

    `split_mode`（**成本从低到高；每页走到够用即止**）：
      - `"rule"`：**只用 L0 规则切分**（零 LLM、零 API 成本）——L0 无产出即该页失败；
      - `"l1"` ：L0 + **L1 指针补全**（模型只报行号、不抄正文，≈¥0.003/页；
        **不调用 L2 兜底**）。实测：L0 **已切对**的页上零增益（例题三页逐位打平）；
        但 L0 **读不出**的页上真能救（1 条兜底 → 4 条记录）；
      - `"auto"`（默认）：**L0 → （仅"L0 读不出"的页）L1 → L2**。
        L0 读得出就直接收工（零成本）；读不出的页先花 ¥0.0036 试 L1
        （比 L2 便宜一半且不生成正文），仍不行才兜底 L2；
      - `"llm"` ：只用线上模型（旧行为）。
    `skip_ocr`：复用已有 `data/structured/<stem>.json`，不重新 OCR
    （L0/L1 只需行与几何；批量重跑因此从"分钟级/页"降到"近乎瞬时"）。

    返回 `{ok, profile_id, rules, n_stems, pages, totals}`。
    `progress(i, n, stem, rec)` 每页完成后回调（供后台任务报进度）。
    """
    import preannotate as PA
    import split_records as sp
    import rule_learn as RL
    from profile_store import get_profile

    sdir = Path(structured_dir or DEFAULT_STRUCTURED_DIR)
    ddir = Path(drafts_dir or DEFAULT_DRAFTS_DIR)
    odir = Path(outbox_dir or DEFAULT_OUTBOX)
    sdir.mkdir(parents=True, exist_ok=True)
    ddir.mkdir(parents=True, exist_ok=True)

    profile = get_profile(profile_id)
    if not profile:
        return {"ok": False, "error": f"档案不存在: {profile_id}",
                "profile_id": profile_id, "pages": [], "totals": {}}

    learned = RL.load(profile_id) if use_rules else {}
    rules = RL.as_prompt_rules(learned) if learned else {}

    ctx = {"PA": PA, "sp": sp, "profile": profile, "profile_id": profile_id,
           "rules": rules, "learned": learned,
           "split_mode": split_mode, "skip_ocr": skip_ocr,
           "prices": llm_prices(),
           "sdir": sdir, "ddir": ddir, "outbox_dir": odir,
           "backup_dir": backup_dir, "retries_ocr": retries_ocr,
           "engine": engine or build_engine(),
           "client_split": split_client or build_split_client()}

    pages: List[dict] = []
    n = len(stems)
    for i, stem in enumerate(stems, 1):
        rec: dict
        try:
            if skip_existing and (ddir / f"{stem}.ai.jsonl").exists():
                rec = {"stem": stem, "skipped": "draft_exists"}
            else:
                rec = one_page(stem, ctx)
        except Exception as e:                            # 兜底：绝不让整批中断
            rec = {"stem": stem, "error": f"unexpected: {e}"}
        pages.append(rec)
        if pages_dir is not None:
            try:
                Path(pages_dir).mkdir(parents=True, exist_ok=True)
                (Path(pages_dir) / f"{stem}.json").write_text(
                    json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
        if progress:
            try:
                progress(i, n, stem, rec)
            except Exception:                             # 进度回调不影响主流程
                pass

    return {"ok": True, "profile_id": profile_id, "n_stems": n,
            "rules": {"used": bool(rules), "summary": RL.summary(learned)},
            "pages": pages, "totals": totals(pages),
            "finished_at": datetime.now().isoformat(timespec="seconds")}


def totals(pages: List[dict]) -> dict:
    """全批汇总（只统计真正跑出草稿的页）。"""
    okp = [p for p in pages
           if not p.get("error") and not p.get("skipped") and p.get("entries")]
    conf, loc = Counter(), Counter()
    for p in okp:
        conf.update(p.get("conf") or {})
        loc.update(p.get("locate") or {})
    n_entries = sum(p.get("entries", 0) for p in okp)
    splitter = Counter(p.get("splitter") or "?" for p in okp)
    l0_conf = Counter((p.get("l0") or {}).get("confidence")
                      for p in okp if p.get("l0"))
    return {
        "pages_total": len(pages), "pages_ok": len(okp),
        "pages_failed": sum(1 for p in pages if p.get("error")),
        "pages_skipped": sum(1 for p in pages if p.get("skipped")),
        "records": sum(p.get("records") or 0 for p in okp),
        "entries": n_entries,
        "anchored": sum((p.get("stats") or {}).get("anchored", 0) for p in okp),
        "values": sum((p.get("stats") or {}).get("values", 0) for p in okp),
        "conf": dict(conf), "locate": dict(loc),
        "n_suspect": sum(p.get("n_suspect", 0) for p in okp),
        "n_flagged": sum(p.get("n_flagged", 0) for p in okp),
        # 拆分器来源分布：`rule_l0` 的页是**零 API 成本**产出的
        "splitter": dict(splitter),
        "l0_conf": {str(k): v for k, v in l0_conf.items()},
        # L1（指针补全）的实测收益与成本——**用真实 token 数字说话**，
        # 不做"应该便宜"的口头结论（用户对线上花费敏感，须可核）
        "l1_filled": sum((p.get("l1") or {}).get("n_filled", 0) or 0 for p in okp),
        "l1_rejected": sum((p.get("l1") or {}).get("n_rejected", 0) or 0 for p in okp),
        "l1_skipped_no_gap": sum(1 for p in okp
                                 if (p.get("l1") or {}).get("reason") == "no_gap"),
        "l1_pages": sum(1 for p in okp if p.get("l1_usage")),
        "l1_tokens_in": sum(((p.get("l1_usage") or {}).get("prompt_tokens") or 0)
                            for p in okp),
        "l1_tokens_out": sum(((p.get("l1_usage") or {}).get("completion_tokens") or 0)
                             for p in okp),
        "l1_cost_cny": round(sum(p.get("l1_cost") or 0 for p in okp), 4),
        # 未被任何记录覆盖的字符（绝不丢字的机械自证；表格/错序页会 >0）
        "orphan_chars": sum((p.get("l0") or {}).get("orphan_chars", 0) or 0
                            for p in okp),
        "high_rate": round(conf.get("high", 0) / n_entries, 4) if n_entries else 0.0,
        "suspect_rate": round(sum(p.get("n_suspect", 0) for p in okp) / n_entries, 4)
                        if n_entries else 0.0,
    }
