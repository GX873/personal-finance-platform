# 资产总览清晰化与图表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让总览直接展示已有真实资产数据，移除陈旧确认阻断，并增加轻量、直观的资产与现金图表。

**Architecture:** 保留现有服务端 viewmodel 和 SVG/CSS 渲染。总览将使用“可计算数据”与“确认状态”分离：确认状态不再阻止金额展示，只有缺失必要数据时金额才保持待补充。分析页只呈现中文现金标签和简短数据说明。

**Tech Stack:** Python 3.12, SQLAlchemy, FastAPI/Jinja2, 原生 SVG/CSS, pytest, Ruff, mypy。

---

### Task 1: 分离可计算总资产与陈旧状态

**Files:**
- Modify: `finance_app/web/viewmodels.py`
- Test: `tests/test_web.py`

- [ ] **Step 1: 写失败测试**：加入一个已有持仓正式净值、现金桶和不完整确认时间的快照，断言 `dashboard()` 返回可计算总额、`advice_action` 不因陈旧阈值变为 `WAIT_FOR_DATA`。
- [ ] **Step 2: 运行测试确认失败**：`py -3.14 -m pytest tests/test_web.py -k "dashboard_uses_known_values_without_stale_confirmation" -q`。
- [ ] **Step 3: 最小实现**：从最新可计算快照/现金桶组成展示总额；将 freshness 仅作为轻量日期标签，不再作为总览金额和日常建议的阻断条件。
- [ ] **Step 4: 运行相关 Web 测试**：`py -3.14 -m pytest tests/test_web.py -q`。

### Task 2: 精简投资分析现金桶与规则文案

**Files:**
- Modify: `finance_app/web/routes.py`
- Modify: `finance_app/templates/analysis.html`
- Test: `tests/test_web.py`

- [ ] **Step 1: 写失败测试**：请求 `/analysis`，断言页面包含“生活备用金”“可投资现金”，不包含 `reserve`、`investment`、`数据陈旧` 或“请确认是否陈旧”。
- [ ] **Step 2: 运行测试确认失败**：`py -3.14 -m pytest tests/test_web.py -k "analysis_cash" -q`。
- [ ] **Step 3: 最小实现**：在 viewmodel/route 中提供中文 bucket label；模板只展示金额、更新时间和简短说明，删除陈旧确认和冗余规则段落。
- [ ] **Step 4: 运行相关 Web 测试**：`py -3.14 -m pytest tests/test_web.py -q`。

### Task 3: 增加资产总览图表

**Files:**
- Modify: `finance_app/web/viewmodels.py`
- Modify: `finance_app/templates/dashboard.html`
- Modify: `finance_app/static/app.css`
- Test: `tests/test_web.py`

- [ ] **Step 1: 写失败测试**：断言 viewmodel 返回现金合计/分配比例和核心/卫星比例；空数据时比例为零且模板显示空状态。
- [ ] **Step 2: 运行测试确认失败**：`py -3.14 -m pytest tests/test_web.py -k "chart or allocation" -q`。
- [ ] **Step 3: 最小实现**：复用现有七日 SVG 趋势数据，增加现金分配堆叠条/环形 SVG 与核心/卫星占比条；所有标签使用中文，数据缺失时显示“暂无可计算数据”。
- [ ] **Step 4: 运行 Web 测试和模板检查**：`py -3.14 -m pytest tests/test_web.py -q` 与 `py -3.14 -m ruff check finance_app tests`。

### Task 4: 全量验证、提交和部署

**Files:**
- Modify: `README.md` or `docs/operations.md` only if behavior documentation needs updating。

- [ ] **Step 1: 运行全量门禁**：`py -3.14 -m pytest -q`、`py -3.14 -m ruff check finance_app tests`、`py -3.14 -m mypy finance_app`、`git diff --check`。
- [ ] **Step 2: 提交变更**：使用中文界面/数据口径对应的提交信息。
- [ ] **Step 3: 推送 GitHub**：普通 `git push origin HEAD:main`，不强推。
- [ ] **Step 4: 部署服务器**：创建新 release，保留 `/var/lib/personal-finance` 和环境文件，运行迁移检查并重启应用及定时器。
- [ ] **Step 5: 线上验收**：确认 release、服务状态、`/health` 和业务表未被改动。
