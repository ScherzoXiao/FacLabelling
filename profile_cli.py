# -*- coding: utf-8 -*-
"""profile_cli —— **档案**（属性表）的命令面（技能包第 4 步 · 2026-09-15）。

用户 2026-09-15 的裁定：「同意补完三个以实现闭合」。

**补的是什么**：技能包形态下没有裁决界面，于是「建档案」与「学规则」两条
原先只活在 Flask 路由里的渠道，命令面够不到（用户 2026-09-14 指出：
「用户是没有提供金标准页的渠道的」）。本模块 + `project_cli` 就是补这两条。

★ 概念边界（侦查实测，**两者不是一回事**）：

| | 存储 | 内容 | 写函数 |
|---|---|---|---|
| **栏目** project | `data/projects.json` | 图片归属 + 模板样式 + ocr_backend | `data_io.create_project`（见 `project_cli`） |
| **档案** profile | `data/collection_profiles/*.json` | **属性表**（name / desc / synonyms） | `profile_store.save_profile`（本模块） |

★★ **「学规则」这一步最容易被漏掉**：标注完金标准页 **≠** `triage` 能用。
判据链条是

    manual_annotations/*.jsonl
      → rule_learn.learn_and_save        → rules_data/learned_<pid>.json
      → rule_split.build_spec(learned, profile)  → spec.anchors
      → page_triage.count_anchors        → 分诊判据

⇒ 没有 `learn` 这一步，`anchors` 为空 ⇒ `has_anchors=False` ⇒
**整批 `unmeasurable`（退出码 3）**。这就是本模块把 `learn` 放进来的理由。

★ **不重造任何一环**（`CLAUDE.md` §60）：
  · 建档 → `profile_store.save_profile` / `upsert_by_name` / `add_attr`
  · 模板解析 → `summary_template.extract_text` + `extract_attr_headers`
  · 学规则 → `rule_learn.learn_and_save`（纯 stdlib、零 API）
  本模块只做「参数 → 调用 → 组织 JSON → 映射退出码」。

退出码
    0   成功
    3   缺料（档案不存在 / 模板文件不存在 / 库里没有金标准页）
    4   校验不过（模板解析不出表头 / 属性数超上限）
    1   内部错误
    64  用法错误

用法
    python profile_cli.py new --name 官报公司註冊 --template "<模板.xlsx>" --json
    python profile_cli.py new --name 官报公司註冊 --attrs 公司名 注册人 资本额
    python profile_cli.py list --json
    python profile_cli.py show --profile prof_xxx --json
    python profile_cli.py add-attr --profile prof_xxx --attr 地址
    python profile_cli.py learn --profile prof_xxx --json
    python profile_cli.py scan --profile prof_xxx --json   # 材料画像（learn 前通读）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 退出码契约：正本在 `cli_contract`（2026-09-24 收敛）----
# ★ 判定点仍在各 `profile_*` 动作内 —— 收敛的是**词表**，不是判定。
from cli_contract import (EXIT_OK, EXIT_INTERNAL, EXIT_MISSING, EXIT_INVALID,  # noqa: E402
                          EXIT_USAGE, EXIT_MEANING, Parser as _Parser)         # noqa: E402


def split_attrs(values) -> List[str]:
    """属性名入参 → 去重列表（容忍全角逗号、容忍 `a,b,c` 与 `a b c` 两种给法）。"""
    out: List[str] = []
    for v in (values or []):
        for part in str(v).replace("，", ",").split(","):
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return out


def attrs_from_template(template_path) -> List[str]:
    """模板文件（.docx/.xlsx/.md）→ 表头属性名列表。

    沿用 `summary_template` 的**同一份**解析（§60），所以命令面读出来的表头
    与 GUI 上传读出来的**逐字一致**。
    """
    import summary_template as SM

    p = Path(template_path)
    text = SM.extract_text(p)                 # 后缀不对/解析失败 → ValueError
    return SM.extract_attr_headers(text)


def _profile_brief(p: dict) -> Dict[str, Any]:
    return {
        "profile_id": p.get("profile_id"),
        "name": p.get("name"),
        "n_attrs": len(p.get("attrs") or []),
        "attrs": [a.get("name") for a in (p.get("attrs") or [])],
        "created_at": p.get("created_at"),
        "template_id": p.get("template_id") or "",
    }


def resolve_profile(token: str, profiles_dir=None) -> Optional[dict]:
    """`--profile` 的值 → 档案 dict。**先按 id 查，查不到再按名称查**。

    这样 agent 拿到用户给的「某类文献档案」这种名字也能直接用，不必先 list。
    """
    import profile_store as PS

    t = (str(token) if token is not None else "").strip()
    if not t:
        return None
    kw = {"profiles_dir": profiles_dir} if profiles_dir is not None else {}
    p = PS.get_profile(t, **kw)
    if p is not None:
        return p
    return PS.find_by_name(t, **kw)


# ============================================================
# 动作
# ============================================================
def _do_new(*, name, description="", template="", attrs=None,
            force_new=False, dry_run=False, profiles_dir=None) -> Dict[str, Any]:
    res: Dict[str, Any] = {
        "ok": False, "dry_run": bool(dry_run), "action": "new",
        "name": str(name or "").strip(), "reused": False,
        "source": "none", "attrs": [], "profile": None, "errors": [],
    }
    n = res["name"]
    if not n:
        # ★ 空名判**校验不过(4)**、不是缺料(3)：前者下一步是「改参数」，
        #   后者是「补材料」——方向相反（§4.3 退出码要能区分下一步动作）。
        res["error"] = "档案名不能为空"
        res["invalid"] = True
        return res

    import profile_store as PS

    kw = {"profiles_dir": profiles_dir} if profiles_dir is not None else {}

    attr_names: List[str] = []
    if template:
        tpl = Path(template)
        if not tpl.exists():
            res["error"] = f"模板文件不存在：{tpl}"
            return res
        try:
            attr_names = attrs_from_template(tpl)
        except ValueError as e:
            res["error"] = f"模板解析失败：{e}"
            res["invalid"] = True
            return res
        except Exception as e:                                # noqa: BLE001
            res["error"] = f"{type(e).__name__}: {e}"
            return res
        if not attr_names:
            res["error"] = ("模板里没解析到表头属性（首个有效行应为各属性列名）")
            res["invalid"] = True
            return res
        res["source"] = "template"
        res["template"] = str(tpl)
    elif attrs:
        attr_names = split_attrs(attrs)
        res["source"] = "attrs"
    else:
        res["source"] = "empty"          # 允许"先建空档案，标注时再补属性"

    res["attrs"] = attr_names

    # ★ 幂等：agent 重试同一条命令不该建出第二个同名档案（§4.2 幂等）。
    existing = PS.find_by_name(n, **kw)
    if existing is not None and not force_new:
        res["ok"] = True
        res["reused"] = True
        res["profile"] = existing
        res["next"] = ("同名档案已存在，**未改动**。要改名/改属性用 "
                       "`profile add-attr`；确实要另立一个用 `--force-new`。")
        return res

    if dry_run:
        res["ok"] = True
        res["next"] = "这是干跑：去掉 --dry-run 即真建档案。"
        return res

    try:
        prof = PS.save_profile({"name": n, "description": description,
                                "attrs": attr_names}, **kw)
    except ValueError as e:
        res["error"] = str(e)
        res["invalid"] = True
        return res
    res["ok"] = True
    res["profile"] = prof
    res["next"] = (f"档案已建：{prof['profile_id']}（{len(attr_names)} 属性）。"
                   "下一步把模板挂到栏目上：`chronicles project template "
                   "--project <栏目id> --file <模板>`，或直接开始标金标准页。")
    return res


def _do_list(*, profiles_dir=None) -> Dict[str, Any]:
    import profile_store as PS

    kw = {"profiles_dir": profiles_dir} if profiles_dir is not None else {}
    profs = PS.list_profiles(**kw)
    return {
        "ok": True, "action": "list", "n_profiles": len(profs),
        "profiles": [_profile_brief(p) for p in profs],
        "next": ("选一个档案 → `chronicles facts --profile <pid>`；"
                 "或看它的属性 → `profile show --profile <pid>`。"),
    }


def _do_show(*, profile, profiles_dir=None, with_lines=False,
             gold_dir=None) -> Dict[str, Any]:
    import rule_learn as RL

    p = resolve_profile(profile, profiles_dir)
    if p is None:
        return {"ok": False, "action": "show",
                "error": f"档案不存在：{profile}（用 `profile list` 看有哪些）"}
    out: Dict[str, Any] = {"ok": True, "action": "show", "profile": p,
                           "attrs": [a.get("name") for a in (p.get("attrs") or [])]}
    # 顺带报「已学规则」的现状 —— 这一步最容易被漏，放在这里让它可见
    learned = RL.load(p["profile_id"])
    out["learned"] = RL.summary(learned)
    if with_lines:
        # ★ 隔离（2026-09-16）：报的必须是**本档案**的金标准页。原先读全目录 ⇒
        #   `profile show` 会把别的档案（乃至测试残留）的页列成本档案的进度。
        gold = RL.load_gold(gold_dir, profile_id=p["profile_id"])
        out["gold_pages"] = sorted(gold.keys())
        out["n_gold_pages"] = len(gold)
        out["n_gold_rows"] = sum(len(v) for v in gold.values())
        out["gold_scope"] = RL.gold_scope_of(RL.read_gold_raw(gold_dir),
                                             p["profile_id"])
    out["next"] = ("`chronicles annotate lines --image <页名>` 提框 → 人定属性 → "
                   "`annotate add` → 然后 `profile learn`（这一步才会产生锚词）。")
    return out


def _do_add_attr(*, profile, attr, description="", profiles_dir=None) -> Dict[str, Any]:
    import profile_store as PS

    p = resolve_profile(profile, profiles_dir)
    if p is None:
        return {"ok": False, "action": "add-attr",
                "error": f"档案不存在：{profile}（用 `profile list` 看有哪些）"}
    kw = {"profiles_dir": profiles_dir} if profiles_dir is not None else {}
    n_before = len(p.get("attrs") or [])
    try:
        prof = PS.add_attr(p["profile_id"], attr, description, **kw)
    except ValueError as e:
        return {"ok": False, "action": "add-attr", "error": str(e), "invalid": True}
    if prof is None:
        return {"ok": False, "action": "add-attr", "error": f"档案不存在：{profile}"}
    added = len(prof.get("attrs") or []) > n_before
    return {"ok": True, "action": "add-attr", "added": added,
            "profile": prof, "attrs": [a.get("name") for a in (prof.get("attrs") or [])],
            "next": ("属性已追加。" if added else
                     "该属性已存在（同名幂等，档案未改动）。"),
            "advice": "" if added else "无需再操作。"}


def _do_learn(*, profile, gold_dir=None, contracts_dir=None,
              rules_dir=None, dry_run=False, profiles_dir=None) -> Dict[str, Any]:
    """金标准 → 规则（★ 这一步产生 `anchors`，没有它 `triage` 一律缺料）。"""
    import rule_learn as RL

    res: Dict[str, Any] = {"ok": False, "action": "learn", "dry_run": bool(dry_run),
                           "profile_id": "", "summary": {}, "errors": []}
    # ⚠ `profiles_dir` 必须一路传到 resolve —— 漏了它，测试隔离会失效、
    #   命令面会回落到**真实**档案目录（这条坑在 app.py 侧已踩过一次）。
    p = resolve_profile(profile, profiles_dir)
    if p is None:
        res["error"] = f"档案不存在：{profile}（用 `profile list` 看有哪些）"
        return res
    pid = p["profile_id"]
    res["profile_id"] = pid

    # ★ 隔离（2026-09-16）：只从**本档案**的例题学。原先读全目录 ⇒
    #   `learned_<pid>.json` 会把别的档案的列宽 / 页边距 / 属性序混进来，
    #   而这份文件正是 `triage` / `plan` 的输入。
    gold = RL.load_gold(gold_dir, profile_id=pid)
    n_pages = len(gold)
    n_rows = sum(len(v) for v in gold.values())
    res["n_gold_pages"] = n_pages
    res["n_gold_rows"] = n_rows
    res["gold_pages"] = sorted(gold.keys())
    res["gold_scope"] = RL.gold_scope_of(RL.read_gold_raw(gold_dir), pid)

    # ★「0 页」是缺料、不是成功 —— 与 `triage`/`ocr` 同一条纪律。
    #   这时 `learn_and_save` 也不会落盘，`anchors` 仍为空，triage 照样 rc 3。
    if n_rows == 0:
        # ★ 隔离后的"0 页"有两种截然不同的成因，必须分开说 —— 否则人会去
        #   "标注新页"，而他真正的问题是**例题标到了别的档案名下**。
        _gs = res.get("gold_scope") or {}
        _other = int(_gs.get("n_rows_other_profile") or 0)
        _unassigned = int(_gs.get("n_rows_unassigned") or 0)
        if _other or _unassigned:
            # ⚠ 这两类**必须分开报**（2026-09-16）：
            #   「标在别的档案名下」= 标错了 → 改归属；
            #   「没有档案标签（未归档）」= 标的时候**没绑定档案** → 先绑定再重存。
            #   原先只有 `_other` 一条岔路，未归档会掉进下面的 else，被报成
            #   「库里还没有任何金标准页」—— **那是假话**（目录里其实有行）。
            _parts = []
            if _other:
                _parts.append(f"{_other} 行标在**别的档案**名下")
            if _unassigned:
                _parts.append(f"{_unassigned} 行**没有档案标签**（未归档）")
            res["error"] = (f"档案 「{pid}」名下**一页金标准都没有** —— "
                            f"库里的行已被隔离排除：" + "；".join(_parts) + "。")
            _tips = []
            if _other:
                _tips.append("标错了档案 → 在界面里重新选档案标注")
            if _unassigned:
                _tips.append("未归档多半是**标注时没绑定档案**（界面顶栏「档案」下拉"
                             "停在「未绑定档案」）→ 先选中本档案，再重新保存那些标注")
            res["next"] = ("`chronicles gold` 看各档案与未归档的页数分布；"
                           + "；".join(_tips) + "。")
        else:
            res["error"] = ("库里还没有任何金标准页（manual_annotations/*.jsonl 为空）——"
                            "学不出规则。")
            res["next"] = ("先用 `chronicles ocr` 补页文本，再 `chronicles annotate "
                           "lines/add` 标注至少 1 页，然后回来 `profile learn`。")
        return res

    if dry_run:
        res["ok"] = True
        # ★ 干跑也要给 diff（G2-②）：`learn()` 是纯函数，不落盘 ⇒ 干跑就能看到
        #   「这一学会把规则改成什么样」。否则人要先落盘、再看变化、不满意再改回来。
        try:
            _new = RL.learn(pid, gold_dir, contracts_dir)
            res["diff"] = RL.diff_learned(RL.load(pid, rules_dir) or {}, _new)
        except Exception as e:                                # noqa: BLE001
            res["diff_error"] = f"{type(e).__name__}: {e}"
        _chg = "会**改变**规则（见 diff）" if (res.get("diff") or {}).get("changed") \
            else "规则**不变**"
        res["next"] = (f"干跑：将从 {n_pages} 页 / {n_rows} 行金标准学规则，"
                       f"落盘 rules_data/learned_{pid}.json，{_chg}。"
                       f"去掉 --dry-run 即真学。")
        return res

    # ★ 变更前快照（G2-②）：`save()` 是**整份覆盖**，不快照就无从回答
    #   「这一圈规则到底哪儿变了」。
    _old = RL.load(pid, rules_dir) or {}
    try:
        learned = RL.learn_and_save(pid, gold_dir, contracts_dir, rules_dir)
    except Exception as e:                                    # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res

    res["ok"] = True
    res["summary"] = RL.summary(learned)
    res["learned_path"] = str(RL.learned_path(pid, rules_dir))
    res["n_pages"] = learned.get("n_pages")
    res["n_rows"] = learned.get("n_rows")
    res["diff"] = RL.diff_learned(_old, learned)
    # 覆盖层：既报"生效了哪几条"，也报"被拒绝的是哪几条"（绝不静默）
    _ov = (learned.get("_meta") or {})
    res["overrides_applied"] = _ov.get("overrides_applied") or []
    res["overrides_skipped"] = _ov.get("overrides_skipped") or []
    res["overrides_noop"] = _ov.get("overrides_noop") or []
    res["overrides_path"] = str(RL.overrides_path(pid, rules_dir))
    _tail = "" if not res["overrides_applied"] else \
        f"（其中 {len(res['overrides_applied'])} 条是人定的、不是学出来的）"
    _nr = learned.get("notes") or {}
    _n_tail = (f"；消费裁决备注 {_nr.get('n_notes', 0)} 条"
               f"（可操作 {_nr.get('n_consumed', 0)} 条）")
    res["next"] = (f"规则已学（骨架 {len(learned['skeleton']['order'])} 项 / "
                   f"锚属性 {learned['skeleton'].get('anchor') or '（无）'}）"
                   f"{_tail}{_n_tail}。"
                   "下一步：`chronicles triage --profile "
                   f"{pid} --project <栏目id>`。")
    return res


def _do_override(*, profile, attr="", position=None, required=None, optional=None,
                 clear=False, show=False, rules_dir=None,
                 page_const_add=None, page_const_remove=None,
                 profiles_dir=None) -> Dict[str, Any]:
    """人工覆盖层（G2-③，2026-09-16 用户裁决）：**pin 属性序** / **强制必填**。

    ★ 与 `learn` 的分工（**别混**）：
      `learn`    = 从金标准**统计**规则，每次整份重写 `learned_<pid>.json`；
      `override` = 人的**声明**，只写 `overrides_<pid>.json`，`learn` 只读它。
    两份文件分开 ⇒ 「学出来的」与「人定的」永远可分辨
    （用户 2026-09-13 定则：AI 的判断不得以库内既有结论的身份被引用）。

    ★ 为什么在这里校验「属性在不在学出的序里」：`apply_overrides` 对非法条目是
      **跳过 + 记账**（它跑在飞轮主链路上，一条坏覆盖不该让学规则停摆）。
      但那意味着写错属性名会**静默不生效到下一次 learn 才被发现** ——
      写入口是人的交互点，在这里当场报 4 才对。
    """
    from datetime import datetime

    import rule_learn as RL

    res: Dict[str, Any] = {"ok": False, "action": "override",
                           "profile_id": "", "errors": []}
    p = resolve_profile(profile, profiles_dir)
    if p is None:
        res["error"] = f"档案不存在：{profile}（用 `profile list` 看有哪些）"
        return res
    pid = p["profile_id"]
    res["profile_id"] = pid

    ov = RL.load_overrides(pid, rules_dir) or {}
    ov.setdefault("profile_id", pid)
    if not isinstance(ov.get("attrs"), dict):
        ov["attrs"] = {}
    attrs: Dict[str, Any] = ov["attrs"]
    res["overrides_path"] = str(RL.overrides_path(pid, rules_dir))
    res["overrides"] = ov

    # ---- 参数互斥（宁可报 4，也不要"看起来设了、其实没设"）----
    if required and optional:
        res["error"] = "--required 与 --optional 互斥（一个设必填、一个取消必填）"
        res["invalid"] = True
        return res
    _setting = (position is not None) or bool(required) or bool(optional)
    _setting_pc = bool(page_const_add or page_const_remove)
    if _setting_pc and (_setting or attr or clear or show):
        res["error"] = ("--page-const-add/--page-const-remove 与属性类参数互斥"
                        "（页常量与属性序/必填是两类覆盖，分开给）")
        res["invalid"] = True
        return res
    if clear and _setting:
        res["error"] = "--clear 不能与 --position / --required / --optional 同时给"
        res["invalid"] = True
        return res
    if show and (clear or _setting):
        res["error"] = "--show 是只读的，不能与设置/清除参数同时给"
        res["invalid"] = True
        return res

    # ---- 清空 ----
    if clear:
        if attr:
            had = attrs.pop(attr, None)
            res["cleared"] = [attr] if had is not None else []
            res["next"] = (f"已清除属性「{attr}」的覆盖，该属性回到金标准统计值。"
                           if had is not None else
                           f"属性「{attr}」本来就没有覆盖（无变化）。")
        else:
            res["cleared"] = sorted(attrs)
            ov["attrs"] = {}
            res["next"] = (f"已清空整份覆盖层（原有 {len(res['cleared'])} 条），"
                           f"规则全部回到金标准统计值。")
        RL.save_overrides(ov, rules_dir)
        res["ok"] = True
        res["n_overrides"] = len(ov["attrs"])
        res["overrides"] = ov
        return res

    # ---- 只看 ----
    if show or (not _setting and not attr and not _setting_pc):
        res["ok"] = True
        res["n_overrides"] = len(attrs)
        pc = ov.get("page_constants") or {}
        res["next"] = (f"当前覆盖层：属性 {len(attrs)} 条、页常量 "
                       f"{len(pc.get('add') or [])} 增/{len(pc.get('remove') or [])} 删"
                       f"（下次 `profile learn` 生效）。"
                       if (attrs or pc) else
                       "当前没有覆盖层 —— 规则全部来自金标准统计。")
        return res

    # ---- 页常量 增 / 删（2026-09-27：与属性覆盖同款纪律）----
    if _setting_pc:
        add_l = [str(w).strip() for w in (page_const_add or []) if str(w).strip()]
        rem_l = [str(w).strip() for w in (page_const_remove or []) if str(w).strip()]
        if not add_l and not rem_l:
            res["error"] = "页常量覆盖的词全是空白 —— 没有可写的声明"
            res["invalid"] = True
            return res
        pc = ov.get("page_constants") if isinstance(ov.get("page_constants"), dict) else {}
        pc = {"add": list(pc.get("add") or []), "remove": list(pc.get("remove") or [])}
        for w in add_l:
            if w not in pc["add"]:
                pc["add"].append(w)
        for w in rem_l:
            if w not in pc["remove"]:
                pc["remove"].append(w)
            if w in pc["add"]:
                pc["add"].remove(w)          # 同词同时增删 ⇒ 以后到者 remove 为准
        pc["source"] = RL.OVERRIDE_SOURCE
        pc["ts"] = datetime.now().isoformat(timespec="seconds")
        ov["page_constants"] = pc
        RL.save_overrides(ov, rules_dir)
        res["ok"] = True
        res["page_constants"] = pc
        res["n_overrides"] = len(attrs)
        res["next"] = ("已写入页常量覆盖：+%s / -%s。下次 `chronicles profile learn "
                       "--profile %s` 生效（页常量不计入任何记录）。"
                       % ("、".join(pc["add"]) or "无", "、".join(pc["remove"]) or "无", pid))
        return res

    # ---- 设置 ----
    if not attr:
        res["error"] = "要给 --attr 才能设置覆盖（只看用 --show）"
        res["invalid"] = True
        return res

    learned = RL.load(pid, rules_dir) or {}
    order = list(((learned.get("skeleton") or {}).get("order")) or [])
    if not order:
        res["error"] = f"档案「{pid}」还没学出规则 —— 没有属性序可 pin。"
        res["next"] = f"先跑 `chronicles profile learn --profile {pid}`，再回来设置覆盖。"
        return res
    if attr not in order:
        res["error"] = f"属性「{attr}」不在学出的属性序里：{' / '.join(order)}"
        res["next"] = ("覆盖层只做 pin 与必填，**不新增属性** —— 要让新属性进入骨架，"
                       "得先标金标准页再 `profile learn`。")
        res["invalid"] = True
        return res

    spec: Dict[str, Any] = dict(attrs.get(attr) or {})
    spec["source"] = RL.OVERRIDE_SOURCE
    spec["ts"] = datetime.now().isoformat(timespec="seconds")
    bits: List[str] = []
    if position is not None:
        if not (1 <= int(position) <= len(order)):
            res["error"] = f"--position {position} 越界（合法 1..{len(order)}）"
            res["invalid"] = True
            return res
        spec["position"] = int(position)
        bits.append(f"钉在第 {int(position)} 位")
    if required:
        spec["required"] = True
        bits.append("必填")
    if optional:
        spec["required"] = False
        bits.append("不再必填")
    attrs[attr] = spec
    RL.save_overrides(ov, rules_dir)

    res["ok"] = True
    res["attr"] = attr
    res["spec"] = spec
    res["overrides"] = ov
    res["n_overrides"] = len(attrs)
    res["next"] = (f"已写入覆盖层：{attr} → {'、'.join(bits)}。"
                   f"下次 `chronicles profile learn --profile {pid}` 生效"
                   f"（叠加在统计产物上，产物里会标明这条是人定的）。")
    return res


def _do_scan(*, profile, structured_dir=None, rules_dir=None,
             pages_glob=None, profiles_dir=None) -> Dict[str, Any]:
    """材料画像（2026-09-29，设计稿 §五）：learn 之前对全池页做无标注通读。

    ★ `--profile` 只作**操作者声明**（写进画像 `_meta` 与文件名），**不校验**
      档案是否已建 —— 画像在 learn 之前、乃至建档案之前都允许先扫
      （「未归档的行不属于任何档案」，归属是人的声明，不是系统的推断）。
    ★ 退出码：structured 缺失/空 ⇒ 3（缺料）；落盘失败 ⇒ 1；成功 ⇒ 0。
    """
    import corpus_scan as CS

    res: Dict[str, Any] = {"ok": False, "action": "scan",
                           "profile_id": str(profile or "").strip(), "errors": []}
    if not res["profile_id"]:
        res["error"] = "--profile 不能为空（画像按操作者声明的档案 id 命名落盘）"
        res["invalid"] = True
        return res
    import preannotate as PA
    sd = Path(structured_dir) if structured_dir else PA.DEFAULT_STRUCTURED_DIR
    res["structured_dir"] = str(sd)
    files = CS.list_pages(sd, pages_glob)
    if not files:
        res["error"] = (f"structured 目录里没有可扫描的页：{sd}"
                        + (f"（glob={pages_glob}）" if pages_glob else ""))
        res["missing"] = True
        res["next"] = ("先 `chronicles ocr` 补页文本（落 data/structured/），"
                       "或检查 --structured-dir / --glob 是否指对。")
        return res
    try:
        report = CS.scan_corpus(sd, profile_id=res["profile_id"],
                                pages_glob=pages_glob)
        path = CS.save_corpus(report, rules_dir)
    except Exception as e:                                    # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res                                            # → 内部错误 1
    res["ok"] = True
    res["scan_path"] = str(path)
    res["n_pages"] = report["_meta"]["n_pages"]
    res["n_constants"] = len(report["constants_candidates"])
    res["n_flagged"] = report["qa"]["n_flagged"]
    res["shape_stats"] = report["shape_stats"]
    res["next"] = (f"画像已落盘 {path.name}（{res['n_pages']} 页）。"
                   f"下一步 `chronicles profile learn --profile {res['profile_id']}`"
                   "（画像里 ratio≥0.9 的常量候选会自动并入页常量，0.4–0.9 仅列出待人审）。")
    return res


def scan_exit_code(result: Dict[str, Any]) -> int:
    """scan 动作的退出码判定（唯一判定点）：0 成功 / 3 缺料 / 4 校验 / 1 内部错误。

    ★ 不能复用 `learn_exit_code`：后者把一切非 invalid 的失败都归 3（缺料），
      而 scan 的**落盘失败必须是 1**（机器/环境问题，补料解决不了，方向不同）。
    """
    if not isinstance(result, dict):
        return EXIT_INTERNAL
    if result.get("ok"):
        return EXIT_OK
    if result.get("invalid"):
        return EXIT_INVALID
    if result.get("missing"):
        return EXIT_MISSING
    return EXIT_INTERNAL


def learn_exit_code(result: Dict[str, Any]) -> int:
    """★ 判定点只有这一处 —— `main` 与 `chronicles.cmd_profile` 都只取用、不重判。

    「0 页金标准」必须是 **3（缺料）**，不能是 0 —— 否则 agent 会以为
    规则学到了，接着跑 triage 拿到一整批 `unmeasurable` 却不知为何。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return EXIT_INVALID if result.get("invalid") else EXIT_MISSING
    return EXIT_OK


def advice_for(result: Dict[str, Any]) -> str:
    if result.get("next"):
        return str(result["next"])
    if result.get("ok"):
        return "完成。"
    return str(result.get("error") or "未知错误")


def format_human(result: Dict[str, Any]) -> str:
    rc = int(result.get("exit_code", learn_exit_code(result)))
    L: List[str] = []
    L.append("=" * 64)
    L.append(f"档案 · {result.get('action')} · 退出码 {rc}（{EXIT_MEANING.get(rc, rc)}）")
    L.append("=" * 64)
    if result.get("action") == "list":
        for p in result.get("profiles") or []:
            L.append(f"  · {p['profile_id']}  {p['name']}"
                     f"  （{p['n_attrs']} 属性）")
        L.append(f"  共 {result.get('n_profiles')} 个档案")
    elif result.get("action") == "learn":
        L.append(f"  档案      : {result.get('profile_id')}")
        L.append(f"  金标准页  : {result.get('n_gold_pages')} 页 / "
                 f"{result.get('n_gold_rows')} 行")
        s = result.get("summary") or {}
        if s.get("skeleton"):
            L.append(f"  骨架      : {' > '.join(s['skeleton'])}")
            L.append(f"  锚属性    : {s.get('anchor') or '（无）'}")
            L.append(f"  页面常量  : {s.get('n_constants')} 条")
        # ---- 规则变更 diff（G2-②）：空 diff 也会打印一行"没变" ----
        d = result.get("diff") or {}
        if result.get("diff_error"):
            L.append(f"  ⚠ 变更对比失败：{result['diff_error']}")
        for ln in d.get("lines") or []:
            L.append(f"  · {ln}")
        # ---- 人工覆盖层（G2-③）：逐条标明来源，被拒的也报 ----
        for a in result.get("overrides_applied") or []:
            L.append(f"  人定覆盖  : {a.get('attr')} · {a.get('field')} → "
                     f"{a.get('value')}（source={a.get('source')}，"
                     f"原值 {a.get('from')}）")
        for a in result.get("overrides_noop") or []:
            L.append(f"  （覆盖无变化）: {a.get('attr')} · {a.get('field')} —— "
                     f"{a.get('reason')}")
        for a in result.get("overrides_skipped") or []:
            L.append(f"  ⚠ 覆盖**未生效** : {a.get('attr')} —— {a.get('reason')}")
    elif result.get("action") == "override":
        L.append(f"  档案      : {result.get('profile_id')}")
        L.append(f"  覆盖层文件: {result.get('overrides_path')}")
        attrs = ((result.get("overrides") or {}).get("attrs")) or {}
        if attrs:
            for k, v in attrs.items():
                bits = []
                if "position" in (v or {}):
                    bits.append(f"第 {v['position']} 位")
                if "required" in (v or {}):
                    bits.append("必填" if v["required"] else "非必填")
                L.append(f"    · {k}: {'、'.join(bits) or '（空声明）'}"
                         f"  [source={(v or {}).get('source')}]")
        else:
            L.append("    （空：规则全部来自金标准统计）")
        if result.get("cleared"):
            L.append(f"  本次清除  : {'、'.join(result['cleared'])}")
        pc = ((result.get("overrides") or {}).get("page_constants")) or {}
        if pc:
            L.append(f"    页常量覆盖: +{'、'.join(pc.get('add') or []) or '无'} / "
                     f"-{'、'.join(pc.get('remove') or []) or '无'}"
                     f"  [source={pc.get('source')}]")
    elif result.get("action") == "scan":
        # 设计稿 §五：一行摘要（页数 / 常量候选数 / 异常页数）
        L.append(f"  档案      : {result.get('profile_id')}（操作者声明）")
        L.append(f"  摘要      : {result.get('n_pages')} 页 / "
                 f"常量候选 {result.get('n_constants')} 条 / "
                 f"异常页 {result.get('n_flagged')} 页")
        if result.get("scan_path"):
            L.append(f"  画像文件  : {result.get('scan_path')}")
    elif result.get("action") == "rules":
        L.append(f"  档案      : {result.get('profile_id')}")
        c = result.get("confirmation")
        if c is None:
            L.append("  规则确认  : ⚠ 未确认 —— 汇报给人审；人确认后用 `profile confirm` 登记")
        elif c.get("matches_current"):
            note = f"（{c['note']}）" if c.get("note") else ""
            L.append(f"  规则确认  : ✅ 已确认（{c.get('confirmed_at')}）{note}，"
                     f"learned sha256 匹配")
        else:
            L.append(f"  规则确认  : ⚠ 曾于 {c.get('confirmed_at')} 确认，"
                     f"但 learned 规则文件已变（sha256 不一致）⇒ 本版规则**未被确认**，"
                     f"重审后重新 `profile confirm`")
        L.append("")
        for ln in (result.get("report_md") or "").splitlines():
            L.append(ln)
    elif result.get("action") == "confirm":
        c = result.get("confirmation") or {}
        L.append(f"  档案      : {result.get('profile_id')}")
        L.append(f"  确认文件  : {result.get('confirm_path')}")
        L.append(f"  确认时刻  : {c.get('confirmed_at')}")
        L.append(f"  绑定规则  : {c.get('learned_file')}  sha256={c.get('learned_sha256','')[:16]}…")
        if c.get("note"):
            L.append(f"  备注      : {c.get('note')}")
    else:
        prof = result.get("profile") or {}
        L.append(f"  档案      : {prof.get('profile_id')}  {prof.get('name')}")
        if result.get("attrs"):
            L.append(f"  属性      : {' / '.join(result['attrs'])}")
        if result.get("reused"):
            L.append("  （同名已存在，已复用，未改动）")
        if result.get("dry_run"):
            L.append("  （干跑：未落盘）")
    errs = result.get("errors") or []
    for e in errs[:10]:
        L.append(f"    · {e}")
    L.append(f"  → {advice_for(result)}")
    return "\n".join(L)


def _do_rules(*, profile, rules_dir=None, profiles_dir=None) -> Dict[str, Any]:
    """识别规则说明书（2026-09-27 用户定则：learn 后系统把总结的规则**汇报给人**，
    人审的是规则本身，不是指标）。

    ★ 渲染 = `rule_report.render`（单一实现）：learned 统计 + overrides 现状
      + 代码判据常量 + notes 消费，全卷到一份人可读说明书里。
    ★ 本动作**只读** —— 它是审阅入口，不是修改入口（修改走 `override`）。
    """
    import rule_learn as RL
    import rule_report as RR

    res: Dict[str, Any] = {"ok": False, "action": "rules", "profile_id": ""}
    p = resolve_profile(profile, profiles_dir)
    if p is None:
        res["error"] = f"档案不存在：{profile}（用 `profile list` 看有哪些）"
        return res
    pid = p["profile_id"]
    res["profile_id"] = pid
    learned = RL.load(pid, rules_dir)
    if not learned:
        res["error"] = f"档案「{pid}」还没学出规则 —— 先跑 `profile learn --profile {pid}`。"
        res["next"] = "learn 之后说明书才有内容（它渲染的就是 learned 的规则）。"
        return res
    try:
        md = RR.render(p, learned, rules_dir)
    except Exception as e:  # noqa: BLE001 —— 渲染失败不能炸命令面
        res["error"] = f"规则说明书渲染失败：{type(e).__name__}: {e}"
        res["invalid"] = True
        return res
    res["ok"] = True
    res["report_md"] = md
    # ---- 规则确认态（2026-09-30，scene ⑥「用户对规则裁决」的落盘读取口）----
    # ★ 只读：本动作不写确认态；登记走 `profile confirm`。
    conf = _read_confirmation(pid, rules_dir)
    if conf is not None:
        conf = dict(conf)
        conf["matches_current"] = (
            conf.get("learned_sha256") == _learned_sha256(pid, rules_dir))
    res["confirmation"] = conf
    res["next"] = ("审阅以上规则（这就是系统读每页的方式）；要改结构项用 "
                   "`profile override`，改完重跑 `profile learn` 生效后再跑批；"
                   "人确认后用 `profile confirm` 登记确认态。")
    return res


def _read_confirmation(profile_id: str, rules_dir=None) -> Optional[dict]:
    """读确认态；不存在 / 损坏 → None（与 `rule_learn.load` 的零影响策略一致）。"""
    import rule_learn as RL
    p = RL.confirmed_path(profile_id, rules_dir)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return d if isinstance(d, dict) and d.get("confirmed_at") else None


def _learned_sha256(profile_id: str, rules_dir=None) -> str:
    import hashlib
    import rule_learn as RL
    p = RL.learned_path(profile_id, rules_dir)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _do_confirm(*, profile, note="", rules_dir=None, profiles_dir=None) -> Dict[str, Any]:
    """规则确认登记（2026-09-30，审计批次 C）：scene ⑥「用户对规则裁决」的落盘口。

    ★ **只登记、不设闸**：triage / facts / plan / exec **不**读这个文件拦人 ——
      把停点从「agent 纪律」升级成「可核验状态」的**最小增量**就是这样：
      `profile rules` 会显示确认态 + learned 文件 sha256 是否仍匹配；
      learn 重跑整份重写 learned ⇒ hash 必变 ⇒ 旧确认自动显示「规则已变，需重审」。
    ★ 确认态文件带 `source:"human"`（同 overrides 的 fail-closed 约定：让"机器写的"
      进不来这个文件——登记动作本身只能由人授意发起）。
    """
    import datetime
    import data_io
    import rule_learn as RL

    res: Dict[str, Any] = {"ok": False, "action": "confirm", "profile_id": ""}
    p = resolve_profile(profile, profiles_dir)
    if p is None:
        res["error"] = f"档案不存在：{profile}（用 `profile list` 看有哪些）"
        res["missing"] = True
        return res
    pid = p["profile_id"]
    res["profile_id"] = pid
    learned = RL.load(pid, rules_dir)
    if not learned:
        res["error"] = f"档案「{pid}」还没有学出的规则 —— 确认态没有对象可绑。"
        res["missing"] = True
        res["next"] = f"先跑 `profile learn --profile {pid}`，确认态绑的是 learned_<pid>.json 的 sha256。"
        return res
    digest = _learned_sha256(pid, rules_dir)   # ★ 复用同一实现（与 rules 的匹配判定同源）
    rec = {
        "profile_id": pid,
        "confirmed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "note": note or "",
        "learned_file": RL.learned_path(pid, rules_dir).name,
        "learned_sha256": digest,
        "source": "human",
    }
    cp = RL.confirmed_path(pid, rules_dir)
    cp.parent.mkdir(parents=True, exist_ok=True)
    data_io.atomic_write_json(cp, rec)
    res["ok"] = True
    res["confirmation"] = rec
    res["confirm_path"] = str(cp)
    res["next"] = ("确认态已落盘（绑定本版 learned 的 sha256）。⚠ 登记口不是机器闸："
                   "triage/exec 不会拦未确认 —— 拦人的仍是「人确认前不得跑批」的 agent 纪律；"
                   "learn 重跑后 hash 必变 ⇒ `profile rules` 将显示「规则已变，需重审」。")
    return res


def run_profile(action: str, **kwargs) -> Dict[str, Any]:
    """跑一个动作，返回结果 + `exit_code`（唯一判定点）。"""
    # CLI 从 argparse 拿到的是 str，而存储层要 Path（`list_profiles` 会调 `.exists()`）
    # —— 在**唯一入口**处统一归一，省得每个动作各写一遍。
    if kwargs.get("profiles_dir"):
        kwargs["profiles_dir"] = Path(kwargs["profiles_dir"])
    table = {
        "new": _do_new, "list": _do_list, "show": _do_show,
        "add-attr": _do_add_attr, "learn": _do_learn,
        "override": _do_override, "rules": _do_rules, "scan": _do_scan,
        "confirm": _do_confirm,
    }
    fn = table.get(action)
    if fn is None:
        r: Dict[str, Any] = {"ok": False, "action": action,
                             "error": f"未知动作：{action}"}
        r["exit_code"] = EXIT_USAGE
        return r
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        r = fn(**kwargs)
    stray = buf.getvalue()
    if stray.strip():
        sys.stderr.write(stray)
    r.setdefault("action", action)
    # ★ 退出码判定按动作分派（scan 与 learn 的缺料/内部错误语义不同，见各函数）
    r["exit_code"] = (scan_exit_code(r) if action == "scan" else learn_exit_code(r))
    return r


# ============================================================
# 入口
# ============================================================
# （`_Parser` = `cli_contract.Parser`，已在文件头随词表一并 import。）


def add_subcommands(sub, func=None) -> None:
    """把全部动作挂到给定的 subparsers 对象上。

    ★ **单一来源**：`chronicles.py` 与本模块的 `build_parser` 都调它 ——
      参数名与帮助文本不会在两处漂移（`CLAUDE.md` §60「勿另造平行实现」）。
    `func` 给定 → 把 `func=<可调用>` 设进每个动作（供 `chronicles` 统一分派）；
    缺省 → 设成**动作名字符串**（供本模块 `main` 自己分派）。
    """

    def _done(p, action):
        p.set_defaults(func=func if func is not None else action)

    def _common(p):
        p.add_argument("--profiles-dir", default=None, help="档案目录（测试隔离用）")
        p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("new", help="建档案（属性来自 --template 文件或 --attrs）")
    p.add_argument("--name", required=True, help="档案名（如 官报公司註冊）")
    p.add_argument("--desc", default="", help="档案描述")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--template", default="",
                   help="模板文件（.docx/.xlsx/.md）—— 取首个有效行作属性集")
    g.add_argument("--attrs", nargs="+", default=None,
                   help="直接给属性名（空格或逗号分隔）")
    p.add_argument("--force-new", action="store_true",
                   help="同名档案存在时仍另建一个（默认幂等复用）")
    p.add_argument("--dry-run", action="store_true", help="只算不落")
    _common(p)
    _done(p, "new")

    p = sub.add_parser("list", help="列出全部档案")
    _common(p)
    _done(p, "list")

    p = sub.add_parser("show", help="看一个档案（属性 + 已学规则现状）")
    p.add_argument("--profile", required=True, help="档案 id 或名称")
    p.add_argument("--with-gold", action="store_true",
                   help="附带报告库里的金标准页清单")
    p.add_argument("--gold", default=None, help="金标准目录（测试隔离用）")
    _common(p)
    _done(p, "show")

    p = sub.add_parser("add-attr", help="给档案追加一个属性（同名幂等）")
    p.add_argument("--profile", required=True, help="档案 id 或名称")
    p.add_argument("--attr", required=True, help="属性名")
    p.add_argument("--desc", default="", help="属性语义说明")
    _common(p)
    _done(p, "add-attr")

    p = sub.add_parser("learn", help="★ 金标准 → 规则（锚词由此产生；漏了它 triage 一律缺料）")
    p.add_argument("--profile", required=True, help="档案 id 或名称")
    p.add_argument("--gold", default=None, help="金标准目录（默认 manual_annotations/）")
    p.add_argument("--contracts", default=None, help="契约目录（默认 data/layout_contracts/）")
    p.add_argument("--rules-dir", default=None, help="规则落盘目录（默认 rules_data/）")
    p.add_argument("--dry-run", action="store_true", help="只算不落（仍显示规则会怎么变）")
    _common(p)
    _done(p, "learn")

    p = sub.add_parser("override",
                       help="人工覆盖层：pin 属性序 / 强制必填（人定 ≠ 学出，分开存放）")
    p.add_argument("--profile", required=True, help="档案 id 或名称")
    p.add_argument("--attr", default="", help="要覆盖的属性名")
    p.add_argument("--position", type=int, default=None,
                   help="把该属性钉在属性序第 N 位（1 起）—— 只重排，不新增属性")
    p.add_argument("--required", dest="req", action="store_true", default=False,
                   help="强制该属性必填")
    p.add_argument("--optional", dest="opt", action="store_true", default=False,
                   help="取消必填（回到非必填）")
    p.add_argument("--page-const-add", action="append", default=None, metavar="词",
                   help="页常量增加（版面固定件，不计入任何记录）；可重复给")
    p.add_argument("--page-const-remove", action="append", default=None, metavar="词",
                   help="页常量移除；可重复给")
    p.add_argument("--clear", action="store_true",
                   help="清空覆盖层（配 --attr 只清该属性；只清属性层，不动页常量层）")
    p.add_argument("--show", action="store_true", help="只看当前覆盖层")
    p.add_argument("--rules-dir", default=None, help="规则目录（默认 rules_data/）")
    _common(p)
    _done(p, "override")

    p = sub.add_parser("scan",
                       help="★ 材料画像：learn 前的全语料无标注通读（确定性统计，零 LLM）")
    p.add_argument("--profile", required=True,
                   help="档案 id 或名称（操作者声明，只写进画像 _meta，不校验归属）")
    p.add_argument("--structured-dir", default=None,
                   help="结构化页目录（默认 data/structured/）")
    p.add_argument("--rules-dir", default=None, help="画像落盘目录（默认 rules_data/）")
    p.add_argument("--glob", default=None,
                   help="页文件通配（fnmatch，如 '0000_*'），默认全部顶层 *.json")
    _common(p)
    _done(p, "scan")

    p = sub.add_parser("rules",
                       help="★ 识别规则说明书：系统将从每页提取什么的完整声明（learn 后人审）")
    p.add_argument("--profile", required=True, help="档案 id 或名称")
    p.add_argument("--rules-dir", default=None, help="规则目录（默认 rules_data/）")
    _common(p)
    _done(p, "rules")

    p = sub.add_parser("confirm",
                       help="★ 规则确认登记：人审完规则书后落盘「本版已确认」"
                            "（绑 learned sha256；登记口，**不是**机器闸）")
    p.add_argument("--profile", required=True, help="档案 id 或名称")
    p.add_argument("--note", default="", help="确认备注（人话结论，原样落盘）")
    p.add_argument("--rules-dir", default=None, help="规则目录（默认 rules_data/）")
    _common(p)
    _done(p, "confirm")


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="profile_cli",
        description="档案（属性表）命令面：建档 / 查档 / 加属性 / **学规则**",
        epilog="退出码：0 成功 · 3 缺料 · 4 校验不过 · 1 内部错误 · 64 用法错误",
    )
    sub = ap.add_subparsers(dest="profile_cmd", required=True, metavar="<action>")
    add_subcommands(sub)
    return ap


def kwargs_from_args(a: argparse.Namespace) -> Dict[str, Any]:
    """Namespace → `run_profile` 的 kwargs。

    ★ **单一来源**：`chronicles.py` 也调它，故 `chronicles profile …` 与
      `profile_cli …` 的**参数名 → kwargs 映射**逐字一致（§60）。
    """
    pd = getattr(a, "profiles_dir", None)
    if a.profile_cmd == "new":
        return {"name": a.name, "description": a.desc, "template": a.template,
                "attrs": a.attrs, "force_new": a.force_new,
                "dry_run": a.dry_run, "profiles_dir": pd}
    if a.profile_cmd == "list":
        return {"profiles_dir": pd}
    if a.profile_cmd == "show":
        return {"profile": a.profile, "profiles_dir": pd,
                "with_lines": a.with_gold, "gold_dir": a.gold}
    if a.profile_cmd == "add-attr":
        return {"profile": a.profile, "attr": a.attr,
                "description": a.desc, "profiles_dir": pd}
    if a.profile_cmd == "override":
        return {"profile": a.profile, "attr": a.attr, "position": a.position,
                "required": a.req, "optional": a.opt, "clear": a.clear,
                "show": a.show, "rules_dir": a.rules_dir,
                "page_const_add": a.page_const_add,
                "page_const_remove": a.page_const_remove, "profiles_dir": pd}
    if a.profile_cmd == "scan":
        return {"profile": a.profile, "structured_dir": a.structured_dir,
                "rules_dir": a.rules_dir, "pages_glob": a.glob, "profiles_dir": pd}
    if a.profile_cmd == "rules":
        return {"profile": a.profile, "rules_dir": a.rules_dir, "profiles_dir": pd}
    if a.profile_cmd == "confirm":
        return {"profile": a.profile, "note": a.note,
                "rules_dir": a.rules_dir, "profiles_dir": pd}
    return {"profile": a.profile, "gold_dir": a.gold,
            "contracts_dir": a.contracts, "rules_dir": a.rules_dir,
            "dry_run": a.dry_run, "profiles_dir": pd}


# 兼容旧名（本模块内部早先叫 _kwargs_for）
_kwargs_for = kwargs_from_args


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(list(argv) if argv is not None else None)
    r = run_profile(a.func, **_kwargs_for(a))
    rc = int(r.get("exit_code", EXIT_INTERNAL))
    if not a.as_json:
        print(format_human(r))
        return rc
    payload = dict(r)
    payload["advice"] = advice_for(r)
    payload["exit_meaning"] = EXIT_MEANING.get(rc)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(format_human(r), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
