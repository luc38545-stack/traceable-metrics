#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""traceable 依赖方向检查（宪法 C-1/C-3/C-4 负面测试的 CI 实现）
规则（REV C.2 附录 T「依赖方向」行）：
  R1 core/        禁止 import plugins.* 与 traceable.apps
  R2 plugins/*    禁止互相 import
  R3 core/        禁止硬编码 /data/commerce 或 /data/research（数据路径只能经 track 参数注入）
  R4 infra/ compose 文件禁止出现对侧轨卷交叉挂载（且 compose 双栈必须存在；
     两轨必须处于隔离网络、不得接入导出桥 bridge-net——ADR-13 ④）
  R5 core/        禁止硬编码存储引擎实现（import duckdb）——只允许在 core/storage/ 内
  R6 plugins/     禁止直连视图取数（SELECT ... FROM <metric>）——取数唯一入口 = core.semantic.api
  R7 core/report/ 报告模板禁止硬编码业务数值（ADR-18：模板源出现非占位数值字面量即构建失败）
  R8 core/audit/  运行台账必须真正 append-only：SQL 里禁止 UPDATE/DELETE/REPLACE
                  （P0-05：状态变化只能追加事件，历史记录一字不可改）
  R9 core/semantic/ 编译期禁止把任意字符串拼进 SQL：标识符必须匹配严格规则，
                  filters 必须是结构化 AST，不得是自由 SQL 片段（P1-06）

本文件是**架构门禁**：只做静态扫描，不需要 duckdb/dbt/浏览器，任何环境都能跑。
运行环境（依赖版本、dbt CLI、playwright、本机服务）的验收在 tests/check_runtime.py。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIOLATIONS: list[str] = []

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
HARDCODED_DATA_RE = re.compile(r"[\"'](/data/(?:commerce|research)|data[/\\](?:commerce|research))[/\\]")
DUCKDB_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+duckdb", re.M)
# R7：先锁定 HTML 模板块（<!DOCTYPE html> … </html>），再取其中的文本节点，
#     最后在剔除了 {...} 插值的纯文案里找独立数值字面量（ADR-18 模板硬编码数字禁令）
_TEMPLATE_BLOCK_RE = re.compile(r'"""\s*<!DOCTYPE html>(.*?)</html>\s*"""', re.S | re.I)
_TEXT_NODE_RE = re.compile(r">([^<>]+)<", re.S)
TEMPLATE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_\-.%])(\d+(?:\.\d+)?)(?![A-Za-z0-9_\-.%])")


def py_files_under(sub: str) -> list[Path]:
    base = ROOT / sub
    return sorted(base.rglob("*.py")) if base.exists() else []


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def check_imports() -> None:
    for p in py_files_under("core"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in IMPORT_RE.finditer(text):
            mod = m.group(1)
            if mod.startswith("plugins"):
                VIOLATIONS.append(f"R1 core→plugins: {rel(p)} imports {mod}")
            if mod.startswith("traceable.apps"):
                VIOLATIONS.append(f"R1 core→apps: {rel(p)} imports {mod}")
    for track in ("commerce", "research"):
        for p in py_files_under(f"plugins/{track}"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            for m in IMPORT_RE.finditer(text):
                mod = m.group(1)
                if mod.startswith("plugins") and not mod.startswith(f"plugins.{track}"):
                    VIOLATIONS.append(f"R2 cross-plugin: {rel(p)} imports {mod}")
    for p in py_files_under("core"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in HARDCODED_DATA_RE.finditer(text):
            VIOLATIONS.append(f"R3 hardcoded data path in core: {rel(p)} -> {m.group(1)}")
    # R5: core 内除 core/storage 外禁止硬编码 duckdb（C-3：存储引擎经接口注入）
    for p in py_files_under("core"):
        if p.parts[-2] == "storage":
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in DUCKDB_IMPORT_RE.finditer(text):
            VIOLATIONS.append(f"R5 hardcoded storage engine in core: {rel(p)} imports duckdb")
    # R6: 插件层禁止直连指标视图取数（唯一取数入口 = core.semantic.api.query_metric）。
    # 视图名 = schemas/metrics/**/*.yml 里的 metric 字段；匹配 "FROM <视图名>" 即违规。
    view_names = []
    for yml in sorted((ROOT / "schemas" / "metrics").rglob("*.yml")):
        try:
            import yaml as _yaml
            doc = _yaml.safe_load(yml.read_text(encoding="utf-8", errors="ignore")) or {}
            if isinstance(doc, dict) and doc.get("metric"):
                view_names.append(doc["metric"])
        except Exception:  # noqa: BLE001
            continue
    view_re = re.compile(r"\bFROM\s+(" + "|".join(map(re.escape, view_names)) + r")\b", re.I) if view_names else None
    if view_re:
        for track in ("commerce", "research"):
            for p in py_files_under(f"plugins/{track}"):
                text = p.read_text(encoding="utf-8", errors="ignore")
                for m in view_re.finditer(text):
                    VIOLATIONS.append(
                        f"R6 direct view query in plugin: {rel(p)} -> FROM {m.group(1)}"
                    )


def _svc_networks(svc: dict) -> set[str]:
    """取一个服务声明的网络名集合（兼容列表字符串 / 字典两种 compose 写法）。"""
    nets = (svc or {}).get("networks", []) or []
    out: set[str] = set()
    for n in nets:
        if isinstance(n, str):
            out.add(n)
        elif isinstance(n, dict):
            out.update(n.keys())
    return out


def check_compose() -> None:
    """R4：卷单轨挂载 + 网络双栈隔离（ADR-13 冻结项②④）。

    卷规则：服务名含 commerce 的卷里出现 research 路径成分 → 违规（反之亦然）。
    网络规则（审计 #15 新增，ADR-13 ④）：
      - 普通轨道服务（commerce/research）必须显式声明网络（默认网桥 = 两轨同网 = 违规）
      - 轨道服务不得接入导出桥 bridge-net（跨轨通信唯一通道，只允许未来导出服务接入）
      - 两轨共享任何同一网络 → 违规
    不存在 infra/compose*.yml 时本身即违规（架构书附录 T：compose 双栈为
    冻结项，缺任何一行对应物 V1 不算开工）。
    """
    import yaml as _yaml

    infra = ROOT / "infra"
    files = sorted(infra.glob("compose*.yml")) if infra.exists() else []
    if not files:
        VIOLATIONS.append("R4 compose 双栈缺失: infra/ 下没有 compose*.yml（ADR-13 冻结项②）")
        return
    for yml in files:
        try:
            data = _yaml.safe_load(yml.read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:  # noqa: BLE001
            VIOLATIONS.append(f"R4 compose 解析失败 {yml.name}: {e}")
            continue
        if not isinstance(data, dict) or "services" not in data:
            VIOLATIONS.append(f"R4 compose 无 services 段: {yml.name}")
            continue
        services = data.get("services") or {}
        com_nets: set[str] = set()
        res_nets: set[str] = set()

        # P1-01 ①：两个独立网络必须在顶层 networks 段真实声明（只写服务引用不算）
        declared = set((data.get("networks") or {}).keys())
        for need in ("traceable-commerce-net", "traceable-research-net",
                     "traceable-bridge-net"):
            if need not in declared:
                VIOLATIONS.append(f"R4 network not declared: {yml.name} 缺 {need}")

        for name, svc in services.items():
            vols = [v if isinstance(v, str) else str(v)
                    for v in ((svc or {}).get("volumes", []) or [])]
            # —— 卷隔离（原规则）——
            for vol_s in vols:
                if "commerce" in name and "research" in vol_s:
                    VIOLATIONS.append(f"R4 cross-volume mount in {yml.name}: {name} mounts {vol_s}")
                if "research" in name and "commerce" in vol_s:
                    VIOLATIONS.append(f"R4 cross-volume mount in {yml.name}: {name} mounts {vol_s}")
            # P1-01 ②：任何服务不得同时挂两轨卷（含 shared-api 这类通用服务）
            if any("commerce" in v for v in vols) and any("research" in v for v in vols):
                VIOLATIONS.append(f"R4 dual-track volumes in {yml.name}: {name}")

            # —— 网络隔离（ADR-13 ④ / P1-01）——
            nets = _svc_networks(svc)
            # 普通轨道服务不得接入导出桥，也不得挂双网（双网 = 事实上的跨轨通道）
            if len(nets) > 1:
                VIOLATIONS.append(
                    f"R4 multi-network service in {yml.name}: {name} -> {sorted(nets)}"
                )
            if any("bridge" in n for n in nets) and name not in ("export-bridge", "bridge"):
                VIOLATIONS.append(f"R4 track service on bridge network: {yml.name} {name}")
            if "commerce" in name:
                com_nets |= nets
                if not nets:
                    VIOLATIONS.append(f"R4 network missing for track service: {yml.name} {name}")
            if "research" in name:
                res_nets |= nets
                if not nets:
                    VIOLATIONS.append(f"R4 network missing for track service: {yml.name} {name}")
        shared = com_nets & res_nets
        if shared:
            VIOLATIONS.append(
                f"R4 track networks overlap (ADR-13): {yml.name} shared={sorted(shared)}"
            )


def _strip_placeholders(text: str) -> str:
    """剔除 {...} 插值表达式（含嵌套花括号）——剩下的才是模板的真实文案。

    例：'口径版本 v{escape(str(x.get("version", 1)))}' → '口径版本 v'
    这样 Python 表达式里的默认值数字不会被误判成硬编码业务数值。
    """
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def check_report_templates() -> None:
    """R7（ADR-18）：报告模板里的数字必须来自结论对象，不得写字面量。

    只检查 HTML 文本节点（>文案< 之间），因此 CSS 块与标签属性不参与判定；
    文本节点内再剔除 {...} 插值，避免误伤 Python 表达式里的默认值。
    """
    base = ROOT / "core" / "report"
    if not base.exists():
        return
    for p in sorted(base.rglob("*.py")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        for block in _TEMPLATE_BLOCK_RE.finditer(src):
            for node in _TEXT_NODE_RE.finditer(block.group(1)):
                plain = _strip_placeholders(node.group(1))
                for num in TEMPLATE_NUMBER_RE.finditer(plain):
                    VIOLATIONS.append(
                        f"R7 hardcoded number in report template: {rel(p)} -> "
                        f"{num.group(1)!r} in {plain.strip()[:40]!r}"
                    )


# ---------------- R8：台账 append-only（P0-05） ----------------

# 只看「SQL 语句开头」的写覆盖动词：这样注释里的说明文字、hashlib 的
# h.update(...) 都不会被误判——用 AST 取字符串常量，天然跳过注释。
_APPENDONLY_BAD_RE = re.compile(r"^\s*(UPDATE|DELETE|REPLACE|TRUNCATE)\b", re.I | re.M)
_APPENDONLY_UPSERT_RE = re.compile(r"\bINSERT\s+OR\s+(REPLACE|IGNORE)\b", re.I)


def _sql_literals(path: Path) -> list[str]:
    """AST 取出源码里的字符串常量，只保留像 SQL 的那些。"""
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError as e:
        VIOLATIONS.append(f"R8 parse error: {rel(path)} -> {e}")
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            up = node.value.upper()
            if any(k in up for k in ("INSERT ", "SELECT ", "CREATE TABLE",
                                     "ALTER TABLE", "UPDATE ", "DELETE ")):
                out.append(node.value)
    return out


def check_append_only() -> None:
    """R8：运行台账只能追加，不能改写/删除历史（D8 + P0-05）。"""
    base = ROOT / "core" / "audit"
    if not base.exists():
        return
    for p in sorted(base.rglob("*.py")):
        for sql in _sql_literals(p):
            m = _APPENDONLY_BAD_RE.search(sql)
            if m:
                VIOLATIONS.append(
                    f"R8 append-only violated: {rel(p)} -> {m.group(1).upper()} "
                    f"({sql.strip().splitlines()[0][:60]})"
                )
            m2 = _APPENDONLY_UPSERT_RE.search(sql)
            if m2:
                VIOLATIONS.append(
                    f"R8 append-only violated: {rel(p)} -> INSERT OR {m2.group(1).upper()} "
                    f"会静默覆盖同名记录"
                )


# ---------------- R9：语义层 SQL 标识符与过滤器安全（P1-06） ----------------

def check_semantic_sql_safety() -> None:
    """R9：编译器/取数端不得把任意字符串拼进 SQL。

    允许 `SELECT dt, value FROM {view}` 这类**必须先经标识符校验**的占位，
    但同一个文件里必须存在标识符校验动作（否则等于裸拼）。
    """
    base = ROOT / "core" / "semantic"
    if not base.exists():
        return
    guard_hints = ("IDENT_RE", "is_identifier", "assert_identifier", "safe_ident")
    for p in sorted(base.rglob("*.py")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        fstring_sql = re.search(r'f"[^"]*\b(SELECT|FROM|WHERE)\b[^"]*\{', src)
        if fstring_sql and not any(h in src for h in guard_hints):
            VIOLATIONS.append(
                f"R9 unsafe SQL interpolation: {rel(p)} 存在 f-string SQL 但无标识符校验"
            )


def main() -> int:
    check_imports()
    check_compose()
    check_report_templates()
    check_append_only()
    check_semantic_sql_safety()
    if VIOLATIONS:
        print("ARCHITECTURE GATE FAILED (R1-R9):")
        for v in VIOLATIONS:
            print("  ✗", v)
        return 1
    print("architecture gate: PASS (R1-R9 clean)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
