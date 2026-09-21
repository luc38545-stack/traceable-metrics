"""结论对象渲染管线（共享核心 · 架构书 §04 L4「报告内核」）。

三条硬约束，全部对应架构书条款：
1. 页眉轨道色带由本内核注入，模板作者不可去除（ADR-14 stamping · 四级之一：文件级）。
2. claims 未过闸一律拒绝渲染（ADR-18：FAIL 即拦截并出对账失败单，绝不静默降级 P4）。
3. 模板源禁止出现业务数值字面量（ADR-18 + CI R7 静态扫描）；所有数字来自结论对象。

诊断书必填段：统计执行器尚未启用（V1 已知边界），因此本版本如实输出
「未检项清单」而不是拿描述性数字冒充检验结论——D5 统计诚实底线。

core 不感知数据位置：导出路径由调用方注入（C-4）。
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from core.audit.ledger import REQUIRED_PROVENANCE
from core.report.schema import ConclusionSchemaError, validate_conclusion

# ADR-14 轨道徽章：文件前缀 + 页眉色带。商用青绿 / 论文灰（§06.3 识别体系）
TRACK_BAND: dict[str, dict[str, str]] = {
    "commerce": {"label": "商用轨", "color": "#0f6f68", "prefix": "[C]"},
    "research": {"label": "论文轨", "color": "#5b656d", "prefix": "[R]"},
}


class ReportBlocked(RuntimeError):
    """报告被闸门拦截（ADR-18：claims 对账不通过即不得进入渲染管线）。"""


def track_band(track: str) -> dict[str, str]:
    if track not in TRACK_BAND:
        raise ValueError(f"invalid track: {track!r}")
    return TRACK_BAND[track]


# ---------------- 结论对象（架构书附录 A · track 必填，D10） ----------------

def build_conclusion(
    ledger: dict[str, Any],
    claims_result: dict[str, Any],
    health_issues: list[dict[str, Any]] | None = None,
    question: str | None = None,
    business_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 run 台账 + 体检 + 对账结果组装成结论对象。

    claims 对账不通过 → 抛 ReportBlocked：不产出结论对象，更不进渲染管线（ADR-18）。
    """
    if claims_result.get("reconcile_diff") != "clean":
        failed = claims_result.get("failed") or []
        raise ReportBlocked(
            f"claims 对账未通过（{len(failed)} 项不一致），报告拒绝渲染: "
            f"{[f.get('period') for f in failed]}"
        )

    track = ledger.get("track")
    series = ledger.get("metric_series") or []
    prov = ledger.get("provenance") or {}
    health = ledger.get("health_summary") or {}
    issues = health_issues or health.get("issue_details") or health.get("issues") or []
    context = business_context or {}
    for role, component in (context.get("components") or {}).items():
        if component.get("reconcile") != "PASS":
            raise ReportBlocked(f"业务解释组件 {role} 未通过对账，拒绝渲染")
        source = component.get("source") or {}
        missing_source = [k for k in ("run_id", "snapshot_id", "query_id") if not source.get(k)]
        if missing_source:
            raise ReportBlocked(
                f"业务解释组件 {role} 缺 source {missing_source}，拒绝渲染"
            )

    conclusion = {
        "conclusion_id": f"ccl-{ledger.get('run_id', 'unknown')}",
        "track": track,                                    # D10：缺此字段拒绝入库
        "question": question or "本次投料的支付成功率是多少？",
        "metric": {
            "name": ledger.get("metric"),
            "version": ledger.get("metric_version", 1),
        },
        "data": {
            "snapshot_id": ledger.get("snapshot_id"),
            "rows_scanned": ledger.get("rows"),
            "series": series,
        },
        "method": {
            "registered_name": "descriptive_ratio",        # V1：描述性口径，非推断统计
            "engine_version": "core@V1",
            "checks": {"statistical_executor": "not_enabled"},
            "diagnosis": None,
        },
        "result": {"effect": None, "p_value": None},
        "claims": claims_result.get("claims", []),
        "llm_audit": {
            "router_model": None,
            "reconcile_diff": claims_result.get("reconcile_diff"),
            "cross_reviewer": "not_enabled",
        },
        "health_issues": issues,
        "business_context": context,
        "provenance": prov,
    }
    # P0-07：复用正式 schema 校验器（track / provenance / metric / series / claims 一次校验完）
    try:
        validate_conclusion(conclusion)
    except ConclusionSchemaError as e:
        raise ReportBlocked(f"结论对象不符合 schema，拒绝渲染：{e}") from e
    return conclusion


# ---------------- 图形（内联 SVG，零前端依赖，可直接打印） ----------------

def series_svg(series: list[list[Any]], color: str) -> str:
    """日粒度序列柱状图。数值全部来自入参，模板不含任何字面量数字。"""
    if not series:
        return ""
    w, h, pad_l, pad_b, pad_t = 640, 200, 46, 34, 12
    vals = [float(v) for _, v in series]
    vmax = max(vals) if vals else 1.0
    vmax = max(vmax, 1e-9)
    n = len(series)
    plot_w = w - pad_l - 12
    plot_h = h - pad_b - pad_t
    slot = plot_w / n
    bw = slot * 0.56
    bars, labels = [], []
    for i, (dt, v) in enumerate(series):
        bh = (float(v) / vmax) * plot_h
        x = pad_l + i * slot + (slot - bw) / 2
        y = pad_t + plot_h - bh
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bw:.2f}" height="{bh:.2f}" '
            f'fill="{color}" opacity="0.85"/>'
            f'<text x="{x + bw / 2:.2f}" y="{y - 5:.2f}" text-anchor="middle" '
            f'font-size="10" fill="#3e464d">{float(v) * 100:.2f}%</text>'
        )
        if n <= 10 or i % max(1, n // 10) == 0:
            labels.append(
                f'<text x="{x + bw / 2:.2f}" y="{h - pad_b + 16:.2f}" text-anchor="middle" '
                f'font-size="10" fill="#5b656d">{escape(str(dt))}</text>'
            )
    grid = "".join(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h * (1 - k / 4):.2f}" x2="{w - 12}" '
        f'y2="{pad_t + plot_h * (1 - k / 4):.2f}" stroke="#e8ebec" stroke-width="1"/>'
        for k in range(5)
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
        f'role="img" aria-label="日粒度序列">{grid}{"".join(bars)}{"".join(labels)}</svg>'
    )


# ---------------- 渲染 ----------------

_STYLE = """
:root{--ink:#16191c;--ink2:#3e464d;--muted:#5b656d;--line:#dde1e3;--soft:#f1f3f3;
--ok:#1c7c46;--warn:#9a6a12;--mono:"Cascadia Code",Consolas,monospace;}
*{box-sizing:border-box;}
body{margin:0;background:#f4f6f6;color:var(--ink);
font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:13.5px;line-height:1.7;}
.band{height:5px;background:var(--band);}
.sheet{max-width:820px;margin:0 auto;background:#fcfcfc;border:1px solid var(--line);
border-top:0;padding:0 0 28px;}
.hd{padding:18px 26px 14px;border-bottom:1px solid var(--line);display:flex;
align-items:baseline;gap:12px;flex-wrap:wrap;}
.hd h1{font-size:19px;font-weight:600;margin:0;}
.badge{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;color:#fff;
background:var(--band);padding:3px 9px;}
.sub{color:var(--muted);font-size:12px;width:100%;margin-top:4px;}
section{padding:16px 26px 4px;}
h2{font-size:13px;font-weight:600;color:var(--muted);letter-spacing:.06em;
text-transform:uppercase;margin:0 0 8px;}
.big{font-family:var(--mono);font-size:32px;font-weight:600;color:var(--band);line-height:1.2;}
.bigcap{font-size:11.5px;color:var(--muted);}
.lead{font-size:15px;color:var(--ink);margin-top:8px;}
.equation{border-left:3px solid var(--band);background:var(--soft);padding:10px 13px;
font-size:13px;color:var(--ink2);}
.note{font-size:12px;color:var(--ink2);margin-top:7px;}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px;}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--soft);}
th{font-size:10.5px;letter-spacing:.05em;color:var(--muted);}
.ok{color:var(--ok);font-weight:600;}
.warn{color:var(--warn);font-weight:600;}
.box{border:1px solid var(--line);background:var(--soft);padding:10px 13px;
font-size:12px;color:var(--ink2);}
details.technical{margin:18px 26px 0;border-top:1px solid var(--line);padding-top:12px;}
details.technical summary{cursor:pointer;color:var(--muted);font-size:12px;font-weight:600;}
.technical-body{margin-top:10px;padding:12px;background:var(--soft);font-size:11px;
color:var(--muted);line-height:1.8;word-break:break-all;}
ul{margin:6px 0 0 18px;padding:0;}
li{margin:3px 0;}
.ft{margin-top:16px;padding:14px 26px 0;border-top:1px solid var(--line);
font-family:var(--mono);font-size:10.5px;color:var(--muted);line-height:1.9;word-break:break-all;}
.wm{text-align:center;font-size:10.5px;color:var(--muted);margin-top:10px;letter-spacing:.04em;}
"""

_UNCHECKED_NOTICE = (
    "这份报告描述当前数据中的结果，没有做实验组对比或显著性检验，"
    "因此不能据此判断某项改动是否造成了差异。"
)


def _format_count(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def render_html(conclusion: dict[str, Any], llm_explanation: dict[str, Any] | None = None) -> str:
    """结论对象 → HTML 报告。

    P0-07：渲染入口同样复用正式 schema 校验器——缺 track / 缺 provenance /
    claims 未过闸一律拒绝，绝不渲染「半份」报告。

    llm_explanation（可选）：Copilot 解释产物（V1-DoD「LLM 解释(claims 过闸)」），
    形态为 {status: "ok", narrative, claims, audit} 或 {status: "not_configured"
    | "gate_failed", reason}。未配置/未过闸时**照样渲染但显性标注**（P4：不静默），
    绝不把未过闸的正文当成品渲染。
    """
    try:
        validate_conclusion(conclusion)
    except ConclusionSchemaError as e:
        raise ReportBlocked(f"结论对象不符合 schema，拒绝渲染：{e}") from e

    claims = conclusion.get("claims") or []
    track = conclusion["track"]
    band = track_band(track)
    metric = conclusion.get("metric") or {}
    data = conclusion.get("data") or {}
    prov = conclusion.get("provenance") or {}
    series = data.get("series") or []
    method = conclusion.get("method") or {}
    business = conclusion.get("business_context") or {}
    display_name = str(business.get("display_name") or metric.get("name") or "指标")
    definition = str(business.get("definition") or "本指标按已登记口径计算。")
    unit = str(business.get("unit") or "项")
    components = business.get("components") or {}

    latest_label, latest_dt = "—", ""
    if series:
        latest_dt = str(series[-1][0])
        latest_label = f"{float(series[-1][1]) * 100:.2f}%"

    numerator = components.get("numerator") or {}
    denominator = components.get("denominator") or {}
    if numerator and denominator:
        num_value = _format_count(numerator.get("value"))
        den_value = _format_count(denominator.get("value"))
        num_label = str(
            numerator.get("sentence_label") or numerator.get("label") or "分子"
        ).removesuffix("数")
        den_label = str(
            denominator.get("sentence_label") or denominator.get("label") or "分母"
        ).removesuffix("数")
        lead = (
            f"{den_value} {unit}{den_label}中，{num_value} {unit}{num_label}，"
            f"{display_name}为 {latest_label}。"
        )
    else:
        lead = f"{latest_dt} 的{display_name}为 {latest_label}。"

    if len(series) <= 1:
        comparison_note = "当前只有一个日期的数据，没有历史或目标值，无法判断比平时更好还是更差。"
    else:
        comparison_note = "下方每日结果可用于观察变化；是否达到业务目标仍需结合目标值判断。"

    rows_html = "".join(
        f"<tr><td>{escape(str(dt))}</td><td style=\"font-family:var(--mono)\">"
        f"{float(v) * 100:.2f}%</td></tr>"
        for dt, v in series
    )

    claims_html = "".join(
        "<tr><td>{p}</td><td style=\"font-family:var(--mono)\">{v}</td>"
        "<td class=\"ok\">{r}</td><td style=\"font-family:var(--mono);font-size:10.5px\">"
        "{s}</td></tr>".format(
            p=escape(str(c.get("period"))),
            v=f"{float(c.get('value', 0)) * 100:.2f}%",
            r=escape(str(c.get("reconcile"))),
            s=escape(
                "{}/{}".format(
                    (c.get("source") or {}).get("snapshot_id", "—"),
                    (c.get("source") or {}).get("query_id", "—"),
                )
            ),
        )
        for c in claims
    )

    component_rows = "".join(
        "<tr><td>{label}</td><td>{value}</td><td class=\"ok\">{status}</td>"
        "<td style=\"font-family:var(--mono);font-size:10.5px\">{source}</td></tr>".format(
            label=escape(str(component.get("label") or role)),
            value=escape(_format_count(component.get("value"))),
            status=escape(str(component.get("reconcile") or "—")),
            source=escape("{}/{}".format(
                (component.get("source") or {}).get("snapshot_id", "—"),
                (component.get("source") or {}).get("query_id", "—"),
            )),
        )
        for role, component in components.items()
    )

    business_issues = business.get("health_issues")
    issues = (
        business_issues if business_issues is not None
        else conclusion.get("health_issues") or []
    )
    issue_lines = []
    for issue in issues:
        if isinstance(issue, dict):
            human = escape(str(issue.get("human_text") or issue.get("code") or "数据问题"))
            impact = escape(str(issue.get("impact") or ""))
            affected = escape(str(issue.get("affected_rows") or "—"))
            suggestion = escape(str(issue.get("fix_suggestion") or ""))
            issue_lines.append(
                f"<li><strong>{human}</strong>；影响：{impact}（涉及 {affected} 行）"
                + (f"；建议：{suggestion}" if suggestion else "") + "</li>"
            )
        else:
            issue_lines.append(f"<li>{escape(str(issue))}</li>")
    issues_html = (
        "<ul>" + "".join(issue_lines) + "</ul>"
        if issue_lines else '<div class="ok">本次体检未发现问题。</div>'
    )
    quality_impact = str(
        business.get("quality_impact") or
        ("数据体检未发现问题。" if not issue_lines else "体检发现问题，请结合下列影响解读。")
    )

    # 智能说明只是可选补充。业务主结论始终由契约 + 已对账 claims 确定性生成；
    # 服务不可用时在技术附录显性标注，但不把技术异常冒充成报告失败。
    if llm_explanation:
        if llm_explanation.get("status") == "ok":
            n_claims = len(llm_explanation.get("claims") or [])
            smart_section = (
                '<section><h2>智能补充说明</h2><div class="box">'
                + escape(str(llm_explanation.get("narrative", ""))) + "</div></section>"
            )
            llm_technical = (
                f"LLM 状态：已生成；Provider={escape(str(llm_explanation.get('provider', '?')))}；"
                f"引用 {n_claims} 个已对账数字；交叉审核/人审升级：未启用。"
            )
        else:
            reason = llm_explanation.get("reason", "LLM Provider 未配置")
            smart_section = ""
            llm_technical = (
                "智能文字说明暂不可用，不影响指标计算和数据核对。"
                f"LLM 状态：{escape(str(llm_explanation.get('status', 'unavailable')))}；"
                f"原因：{escape(str(reason))}。"
            )
    else:
        smart_section = ""
        llm_technical = (
            "智能文字说明暂不可用，不影响指标计算和数据核对。"
            "LLM 状态：not_configured。"
        )

    unchecked = "".join(
        f"<li>{escape(str(k))}：{escape(str(v))}</li>"
        for k, v in (method.get("checks") or {}).items()
    )
    component_table = (
        "<h3>分子/分母 claims</h3><table><thead><tr><th>指标</th><th>值</th>"
        "<th>对账</th><th>来源</th></tr></thead><tbody>" + component_rows + "</tbody></table>"
        if component_rows else ""
    )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(display_name)} · TraceableMetrics 报告</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="band" style="--band:{band['color']}"></div>
<div class="sheet" style="--band:{band['color']}">

  <div class="hd">
    <h1>{escape(display_name)}报告</h1>
    <span class="badge">{escape(band['label'])}</span>
    <div class="sub">数据日期 {escape(latest_dt)} · 基于 {escape(str(data.get('rows_scanned', '—')))} 行明细</div>
  </div>

  <section>
    <h2>结论先看</h2>
    <div class="big">{escape(latest_label)}</div>
    <div class="lead">{escape(lead)}</div>
  </section>

  <section>
    <h2>这个指标怎么算</h2>
    <div class="equation">{escape(definition)}</div>
    <div class="note">按订单口径计算，不是按金额计算。</div>
  </section>

  <section>
    <h2>这代表什么</h2>
    <div class="box">{escape(lead)}<br>{escape(comparison_note)}</div>
  </section>

  <section>
    <h2>数据质量</h2>
    <div class="box">{escape(quality_impact)}</div>
    {issues_html}
  </section>

  <section>
    <h2>每日结果</h2>
    {series_svg(series, band['color'])}
    <table><thead><tr><th>日期</th><th>{escape(display_name)}</th></tr></thead><tbody>{rows_html}</tbody></table>
  </section>

  <section>
    <h2>数据可信度</h2>
    <div class="box ok">本报告中的数字已使用同一份数据快照重新计算并核对一致，可以追溯。</div>
  </section>

  {smart_section}

  <section>
    <h2>解读边界</h2>
    <div class="box">{escape(_UNCHECKED_NOTICE)}</div>
  </section>

  <details class="technical">
    <summary>技术附录（需要追溯或排错时展开）</summary>
    <div class="technical-body">
      TRACK · {escape(band['label'])} · 指标 ID {escape(str(metric.get('name', '—')))}
      · 口径版本 v{escape(str(metric.get('version', 1)))}<br>
      数字对账（ADR-18）：
      <table><thead><tr><th>周期</th><th>值</th><th>对账</th><th>来源</th></tr></thead>
      <tbody>{claims_html}</tbody></table>
      {component_table}
      <h3>智能说明状态</h3><div>{llm_technical}</div>
      <h3>方法与未检项</h3><div>registered_name={escape(str(method.get('registered_name', '—')))}</div>
      <div>未检项清单</div><ul>{unchecked or '<li>无</li>'}</ul>
      <h3>溯源标识</h3>
      run_id {escape(str(prov.get('run_id', '—')))} ·
      snapshot_id {escape(str(prov.get('snapshot_id', '—')))} ·
      batch_id {escape(str(prov.get('batch_id', '—')))}<br>
      commit_sha {escape(str(prov.get('commit_sha', '—')))}<br>
      raw_sha256 {escape(str(prov.get('raw_sha256', '—')))}<br>
      结论对象 {escape(str(conclusion.get('conclusion_id', '—')))} · 生成于 {escape(generated)}
    </div>
  </details>
  <div class="wm">TRACEABLE · 数字可追溯到 raw 文件 · 内部决策参考</div>
</div>
</body>
</html>
"""
