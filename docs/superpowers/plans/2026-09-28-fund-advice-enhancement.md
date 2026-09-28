# 基金机会提醒与现金流规则增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with test checkpoints.

**目标：** 为个人理财平台增加 15 日工资周期、特殊机会预算、14:00 可配置提醒、周末和法定节假日跳过、手工净值可编辑，以及基于 `efinance`/`xalpha` 适配器的机会筛选基础。

**架构：** 保持现有 FastAPI + SQLAlchemy + SQLite + systemd 单体架构。现金流和日历规则放在可测试的纯 Python 模块中；行情接入通过统一的适配器接口隔离第三方库；正式净值和盘中估算使用不同的数据类型；建议引擎只输出条件式动作，不执行订单。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy、Alembic、Jinja2、pytest、`efinance`/`xalpha` 可选依赖。

---

### Task 1: 增加工资周期和特殊机会预算模型

**Files:**
- Modify: `finance_app/ledger/budget.py`
- Modify: `finance_app/portfolio/rules.py`
- Test: `tests/ledger/test_budget.py`
- Test: `tests/portfolio/test_rules.py`

- [ ] **Step 1: 写失败测试，锁定 15 日周期和预算顺序**

```python
def test_salary_cycle_starts_on_fifteenth_and_ends_before_next_fifteenth():
    assert salary_cycle(date(2026, 9, 15)) == (date(2026, 9, 15), date(2026, 10, 14))
    assert salary_cycle(date(2026, 10, 14)) == (date(2026, 9, 15), date(2026, 10, 14))
    assert salary_cycle(date(2026, 10, 15)) == (date(2026, 10, 15), date(2026, 11, 14))


def test_replenishment_blocks_investment_and_special_budget_until_reserve_is_full():
    allocation = cycle_allocation(
        reserve_cents=90000,
        reserve_target_cents=100000,
        normal_budget_cents=20000,
        special_used=False,
    )
    assert allocation.reserve_replenishment_cents == 10000
    assert allocation.normal_investment_cents == 0
    assert allocation.special_opportunity_cents == 0


def test_special_opportunity_is_limited_to_one_hundred_yuan_once_per_cycle():
    assert special_opportunity_budget(100000, 20000, used=False) == 10000
    assert special_opportunity_budget(100000, 20000, used=True) == 0
```

- [ ] **Step 2: 运行失败测试**

运行：`pytest tests/ledger/test_budget.py tests/portfolio/test_rules.py -q`

预期：失败，因为周期和特殊预算函数尚不存在。

- [ ] **Step 3: 实现最小纯函数**

在 `budget.py` 中新增 `salary_cycle(day) -> tuple[date, date]`、`CycleAllocation` 和 `cycle_allocation(...)`；周期使用 `day.day >= 15` 判断起点，跨年时用 `date.replace` 正确计算次月 14 日。新增 `special_opportunity_budget(reserve_cents, normal_budget_cents, used)`，只返回不超过 10000 分且不超过当前储备金可用超额的金额。

在 `rules.py` 的 `RuleContext` 增加 `special_opportunity_used`、`special_opportunity_cents` 和 `reserve_replenishment_cents` 字段；当储备金低于目标时，`evaluate_new_investment` 返回 `WAIT`，并且特殊机会函数返回 0。

- [ ] **Step 4: 运行测试并检查回归**

运行：`pytest tests/ledger/test_budget.py tests/portfolio/test_rules.py -q`

预期：新增测试和现有现金流/规则测试全部通过。

- [ ] **Step 5: 提交**

```powershell
git add finance_app/ledger/budget.py finance_app/portfolio/rules.py tests/ledger/test_budget.py tests/portfolio/test_rules.py
git commit -m "feat: add salary cycle and special opportunity budget rules"
```

### Task 2: 添加可维护的中国交易日历和提醒时间规则

**Files:**
- Create: `finance_app/calendar.py`
- Modify: `finance_app/cli.py`
- Modify: `finance_app/web/routes.py`
- Modify: `finance_app/templates/settings.html`
- Modify: `finance_app/ops/export.py`
- Test: `tests/test_daily_check.py`
- Test: `tests/web/test_settings.py`

- [ ] **Step 1: 写失败测试**

```python
def test_default_daily_schedule_is_fourteen_hundred():
    assert DEFAULT_SCHEDULE == "14:00"


@pytest.mark.parametrize("day", [date(2026, 9, 26), date(2026, 9, 27)])
def test_scheduled_check_skips_weekends(day, session, fixed_clock):
    check = DailyCheck(session, clock=fixed_clock(day, 14, 0))
    assert check.run_scheduled() is DailyCheckResult.NOT_DUE


def test_scheduled_check_skips_configured_chinese_holiday(session, fixed_clock):
    session.add(AppSetting(key="cn_holidays", value='["2026-10-01"]'))
    session.commit()
    check = DailyCheck(session, clock=fixed_clock(date(2026, 10, 1), 14, 0))
    assert check.run_scheduled() is DailyCheckResult.NOT_DUE


def test_settings_default_and_override_use_fourteen_hundred(client, db_session):
    login(client)
    response = client.get("/settings")
    assert 'value="14:00"' in response.text
    response = client.post("/settings", data={"daily_schedule": "15:30", ...})
    assert response.status_code == 303
```

- [ ] **Step 2: 运行失败测试**

运行：`pytest tests/test_daily_check.py tests/web/test_settings.py -q`

预期：默认值测试失败，周末和节假日测试会进入任务执行路径。

- [ ] **Step 3: 实现日历模块和调度判断**

在 `calendar.py` 中新增：

```python
def is_skipped_reminder_day(day: date, holidays: frozenset[date]) -> bool:
    return day.weekday() >= 5 or day in holidays


def parse_holidays(value: str) -> frozenset[date]:
    payload = json.loads(value or "[]")
    return frozenset(date.fromisoformat(item) for item in payload)
```

在 `DailyCheck.run_scheduled()` 中读取 `cn_holidays`，先对本地日期执行周末/节假日跳过，再执行五分钟时间窗口。保留现有的前一天窗口以处理跨午夜唤醒，但不允许节假日进入 `run()`。

将 `DEFAULT_SCHEDULE` 和设置页默认值改为 `14:00`，把 `cn_holidays` 加入安全导出设置键。设置页增加节假日 JSON 的合法性校验和当前加载状态显示。

- [ ] **Step 4: 运行测试**

运行：`pytest tests/test_daily_check.py tests/web/test_settings.py tests/ops/test_export.py -q`

预期：默认时间、设置覆盖、周末、节假日和导出测试全部通过。

- [ ] **Step 5: 提交**

```powershell
git add finance_app/calendar.py finance_app/cli.py finance_app/web/routes.py finance_app/templates/settings.html finance_app/ops/export.py tests/test_daily_check.py tests/web/test_settings.py tests/ops/test_export.py
git commit -m "feat: skip reminders on weekends and holidays"
```

### Task 3: 让手工净值可编辑并保留审计记录

**Files:**
- Modify: `finance_app/web/routes.py`
- Modify: `finance_app/templates/price_form.html`
- Modify: `finance_app/portfolio/models.py` only if an edit metadata column is required by migration
- Create: `alembic/versions/0003_manual_price_edit_audit.py` only if schema changes are needed
- Test: `tests/web/test_forms.py`

- [ ] **Step 1: 将现有冲突测试改成失败的编辑行为测试**

```python
def test_manual_price_can_be_edited_and_audits_old_and_new_values(client, db_session):
    login(client)
    create_manual_price(client, asset_id=1, price="1.00000000", valuation_date="2026-09-23")
    snapshot = db_session.scalar(select(PriceSnapshot))
    response = client.get(f"/prices/{snapshot.id}/edit")
    assert response.status_code == 200
    response = client.post(
        f"/prices/{snapshot.id}",
        data={"price": "1.20000000", "valuation_date": "2026-09-23", "source": "manual:fund-statement", "csrf_token": csrf(client, f"/prices/{snapshot.id}/edit")},
    )
    assert response.status_code == 303
    assert db_session.get(PriceSnapshot, snapshot.id).price == Decimal("1.2")
    event = db_session.scalar(select(AuditEvent).where(AuditEvent.event_type == "price.updated"))
    assert event is not None
    assert event.summary["old_price"] == "1.00000000"
    assert event.summary["new_price"] == "1.20000000"
```

- [ ] **Step 2: 运行失败测试**

运行：`pytest tests/web/test_forms.py::test_manual_price_can_be_edited_and_audits_old_and_new_values -q`

预期：404，因为编辑路由不存在。

- [ ] **Step 3: 实现编辑路由和模板列表**

在 `routes.py` 增加受登录和 CSRF 保护的 `GET /prices/{id}/edit` 和 `POST /prices/{id}`。仅允许 `source.startswith("manual:")` 的记录编辑；外部来源返回 403。保存旧值、日期、来源和新值到 `AuditEvent.summary`，保持同一唯一键冲突校验。

在 `price_form.html` 增加最近手工净值表格和编辑链接；编辑表单复用新增表单的校验和错误展示。

- [ ] **Step 4: 更新旧测试和运行净值页面测试**

运行：`pytest tests/web/test_forms.py -q`

预期：原有幂等新增测试仍通过，原“不同价格冲突”断言改为编辑成功，非法价格/日期/来源测试仍通过。

- [ ] **Step 5: 提交**

```powershell
git add finance_app/web/routes.py finance_app/templates/price_form.html tests/web/test_forms.py
git commit -m "feat: allow audited manual price edits"
```

### Task 4: 建立行情适配器接口和正式/估算净值分层

**Files:**
- Modify: `finance_app/market/base.py`
- Modify: `finance_app/market/eastmoney.py`
- Modify: `finance_app/market/service.py`
- Create: `finance_app/market/efinance_adapter.py`
- Create: `finance_app/market/xalpha_adapter.py`
- Modify: `finance_app/portfolio/models.py`
- Create: `alembic/versions/0003_market_quote_metadata.py` if migration numbering remains available after Task 3
- Test: `tests/market/test_service.py`
- Test: `tests/market/test_eastmoney.py`
- Create: `tests/market/test_adapters.py`

- [ ] **Step 1: 写失败测试，锁定 Quote 数据类型和降级行为**

```python
def test_quote_distinguishes_official_nav_from_intraday_estimate():
    official = MarketQuote(..., quote_type=QuoteType.OFFICIAL_NAV)
    estimate = MarketQuote(..., quote_type=QuoteType.INTRADAY_ESTIMATE)
    assert official.quote_type is QuoteType.OFFICIAL_NAV
    assert estimate.quote_type is QuoteType.INTRADAY_ESTIMATE


def test_adapter_failure_returns_wait_for_data_without_fabricating_price():
    adapter = EfinanceAdapter(client=FailingClient())
    result = adapter.fetch("000001")
    assert result.status == "error"
    assert result.price is None
```

- [ ] **Step 2: 运行失败测试**

运行：`pytest tests/market/test_adapters.py -q`

预期：失败，因为统一 Quote 类型和适配器不存在。

- [ ] **Step 3: 实现接口和可选依赖**

在 `market/base.py` 定义 `QuoteType`、`MarketQuote` 和 `FundMarketAdapter` 协议。`efinance_adapter.py` 使用延迟导入：未安装 `efinance` 时返回结构化依赖缺失错误；安装后只调用公开基金查询接口并统一字段。`xalpha_adapter.py` 提供历史序列转换、收益、回撤和波动率方法；未安装时返回明确的可选依赖错误。

在 `PriceSnapshot` 增加 `quote_type` 字段，默认 `OFFICIAL_NAV`，并通过 Alembic 迁移为现有数据填充正式净值。`FundPriceService` 写入来源、抓取时间和类型，估算净值不能覆盖正式净值。

- [ ] **Step 4: 运行行情和数据库测试**

运行：`pytest tests/market tests/test_database.py -q`

预期：行情服务、依赖缺失降级、迁移和现有 EastMoney 测试全部通过。

- [ ] **Step 5: 提交**

```powershell
git add finance_app/market finance_app/portfolio/models.py alembic/versions tests/market tests/test_database.py
git commit -m "feat: add market adapter and quote type boundaries"
```

### Task 5: 增加防追高的机会筛选和周期去重

**Files:**
- Modify: `finance_app/portfolio/rules.py`
- Modify: `finance_app/cli.py`
- Modify: `finance_app/portfolio/models.py`
- Create: `finance_app/portfolio/opportunities.py`
- Create: `alembic/versions/0004_opportunity_alerts.py`
- Test: `tests/portfolio/test_rules.py`
- Create: `tests/portfolio/test_opportunities.py`
- Modify: `tests/test_daily_check.py`

- [ ] **Step 1: 写失败测试**

```python
def test_candidate_is_rejected_when_recent_rally_is_overheated():
    result = evaluate_opportunity(candidate_with(recent_gain_bps=1800, drawdown_bps=0))
    assert result.action is OpportunityAction.WATCH
    assert result.reason_code == "RECENT_RALLY"


def test_candidate_is_eligible_when_allocation_gap_and_valuation_are_favorable():
    result = evaluate_opportunity(candidate_with(allocation_gap_bps=1200, valuation_percentile=45, recent_gain_bps=100, drawdown_bps=700))
    assert result.action is OpportunityAction.BUY_IN_BATCHES
    assert result.amount_cents == 10000


def test_same_opportunity_is_not_emitted_twice_in_one_salary_cycle(session):
    assert opportunity_already_emitted(session, code="000001", cycle_start=date(2026, 9, 15)) is False
    record_opportunity(session, code="000001", cycle_start=date(2026, 9, 15))
    assert opportunity_already_emitted(session, code="000001", cycle_start=date(2026, 9, 15)) is True
```

- [ ] **Step 2: 运行失败测试**

运行：`pytest tests/portfolio/test_opportunities.py tests/portfolio/test_rules.py -q`

预期：失败，因为机会模型、筛选函数和周期去重表不存在。

- [ ] **Step 3: 实现机会筛选**

在 `opportunities.py` 定义候选输入和结果类型，按组合缺口、重复度、风险等级、估值分位、近期涨幅、回撤、历史长度和可用现金依次筛选。估值高位、快速上涨或快速下跌未企稳均返回 `WATCH`，不产生买入金额。满足条件时返回最多 10000 分的 `BUY_IN_BATCHES`。

在 `portfolio/models.py` 增加 `OpportunityAlert`，字段包括代码、工资周期起始日、动作、金额、触发原因、数据时间、是否使用特殊预算和创建时间，并对 `(code, cycle_start)` 建唯一约束。

在 `DailyCheck` 中于普通摘要前执行机会筛选；只有完成储备金补足、周期特殊预算未使用且数据新鲜时才允许特殊机会。记录后发送一次即时邮件，重复执行只读取已有记录。

- [ ] **Step 4: 运行测试**

运行：`pytest tests/portfolio tests/test_daily_check.py -q`

预期：防追高、资金上限、周期去重和现有每日任务测试全部通过。

- [ ] **Step 5: 提交**

```powershell
git add finance_app/portfolio finance_app/cli.py alembic/versions tests/portfolio tests/test_daily_check.py
git commit -m "feat: add event-driven fund opportunity screening"
```

### Task 6: 更新提醒页面、说明文案和部署默认值

**Files:**
- Modify: `finance_app/templates/settings.html`
- Modify: `finance_app/templates/alerts.html`
- Modify: `finance_app/templates/dashboard.html`
- Modify: `deploy/finance-daily.service`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Test: `tests/test_web.py`
- Test: `tests/deploy/test_artifacts.py`

- [ ] **Step 1: 写失败测试**

```python
def test_alert_page_labels_rule_based_advice_not_prediction(client):
    login(client)
    response = client.get("/alerts")
    assert "条件式建议" in response.text
    assert "不构成涨跌预测" in response.text


def test_deployment_defaults_use_fourteen_hundred():
    service = Path("deploy/finance-daily.service").read_text(encoding="utf-8")
    assert "14:00" in service or "daily_schedule" in service
```

- [ ] **Step 2: 运行失败测试**

运行：`pytest tests/test_web.py tests/deploy/test_artifacts.py -q`

预期：提醒页和文档仍使用旧的 09:00/预测表述。

- [ ] **Step 3: 更新界面和文档**

在设置页显示工资日 15 日、默认 14:00、节假日状态、储备金目标 1000 元、最低余额 900 元和特殊机会上限 100 元。提醒页显示动作、资金来源、正式/估算标签、来源时间和风险纪律，并固定显示“不构成涨跌预测”。更新 README 和运维文档中的调度说明。

- [ ] **Step 4: 运行页面和部署测试**

运行：`pytest tests/test_web.py tests/deploy/test_artifacts.py -q`

预期：页面、部署清单和旧认证/安全测试全部通过。

- [ ] **Step 5: 提交**

```powershell
git add finance_app/templates deploy README.md docs/operations.md tests/test_web.py tests/deploy/test_artifacts.py
git commit -m "docs: explain rule-based fund advice and new schedule"
```

### Task 7: 集成测试、迁移验证和发布检查

**Files:**
- Modify: `scripts/release-check.sh` if required by new checks
- Test: all existing tests under `tests/`

- [ ] **Step 1: 运行迁移和全量测试**

运行：`alembic upgrade head`，随后运行 `pytest -q`。

预期：数据库迁移成功，全量测试通过。

- [ ] **Step 2: 运行静态和差异检查**

运行：`ruff check finance_app tests`、`git diff --check`、`bash scripts/release-check.sh`。

预期：无 lint 错误、无空白错误、发布检查通过。

- [ ] **Step 3: 验证关键行为**

运行：`python -m finance_app.cli daily-check --dry-run`，检查输出包含当前周期、储备金状态、正式/估算数据状态和建议动作；使用测试数据库分别验证周末、节假日、储备金补足和特殊机会去重。

- [ ] **Step 4: 提交集成结果**

```powershell
git add scripts/release-check.sh
git commit -m "test: verify fund advice enhancement release"
```

