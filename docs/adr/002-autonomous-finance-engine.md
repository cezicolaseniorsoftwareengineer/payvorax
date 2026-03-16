# ADR 002 — Autonomous Finance Engine (Bio Code Tech Pay)

**Status:** Approved — Implementation in progress
**Date:** 2026-03-16
**Author:** Cezi Cola Senior Software Engineer

---

## 1. Context

The Bio Code Tech Pay platform has:

- A working IA chat module (`app/ia/router.py`) that proxies to OpenRouter (gpt-4o-mini)
- A subscription plan (`app/minha_conta/`) — R$9.90/month, the "gerente 23h" product
- A `get_financial_health()` function (`app/minha_conta/service.py`) that computes health score, balance, sent/received totals
- Full PIX transaction history in `transacoes_pix` table (`PixTransaction` model)
- Boleto and credit card records

**Problem:** The IA agent is completely blind to account data. It cannot answer "how much did I spend this month?" or "should I invest my balance?". It is a generic advisor without context.

**Goal:** Transform the IA into a Wealth Operating System — an autonomous financial agent that:
1. Has full read access to the user's movements (deterministic data layer)
2. Generates financial insights automatically (engine layer)
3. Communicates decisions in natural language (AI layer — read-only, never executes)

---

## 2. Decision

**Adopt a 5-phase implementation plan** to evolve the existing IA module into the Autonomous Finance Engine.

Architecture invariant: **all financial decisions are deterministic and auditable. The LLM layer only reads and explains. It never writes to the database, never executes transactions, and never holds account state.**

```
[Database: PixTransaction, BoletoTransaction, User]
                     |
         [Financial Context Builder]  <-- deterministic
                     |
         [Finance Engine Services]   <-- deterministic
            - CashflowAnalyzer
            - WealthScoreEngine
            - StrategyEngine
            - SimulationEngine
            - OpportunityEngine
                     |
         [AI Advisor Layer]          <-- LLM reads, explains, suggests
                     |
         [User Interface]            <-- Jinja2 templates
```

---

## 3. Implementation Phases

### Phase 1 — Financial Context Injection (Priority: Critical)

**What:** Inject real account data into every IA chat request as structured context.

**Files to create/modify:**
- NEW: `app/ia/financial_context.py` — service that builds the financial snapshot from DB
- MODIFY: `app/ia/router.py` — inject DB session + call financial_context before calling OpenRouter

**Context injected per request:**
```python
{
  "balance": 1420.50,
  "last_30d_received": 3800.00,
  "last_30d_sent": 2380.50,
  "last_30d_net": 1419.50,
  "total_transactions_30d": 12,
  "health_score": 74,
  "health_label": "Bom",
  "top_recent_transactions": [...]  # last 5, masked
}
```

**Impact:** The IA can now answer: "Voce recebeu R$3.800 e gastou R$2.380 nos ultimos 30 dias. Sobrou R$1.419." This is the foundation for all subsequent phases.

**Security:** No PII in LLM context. CPF/CNPJ masked. Names truncated. No keys or tokens.

---

### Phase 2 — Financial Profile + Wealth Score Engine (Priority: High)

**What:** Replace the simple `health_score` (currently in `minha_conta/service.py`) with a deterministic, multi-dimensional wealth score (0-100).

**Files to create:**
- NEW: `app/ia/finance_engine.py` — all deterministic financial calculation functions

**Wealth Score formula (deterministic):**
```
score = 0
+ savings_rate_score     (0-25)  # net_30d / received_30d
+ liquidity_score        (0-20)  # balance / monthly_expenses
+ activity_score         (0-15)  # transaction regularity
+ verification_score     (0-15)  # email + doc verified
+ growth_trend_score     (0-25)  # trailing 3-month net trend
```

**Outputs:**
- `wealth_score`: int 0-100
- `savings_capacity`: float (monthly surplus)
- `financial_stability_index`: float (0.0 to 1.0)
- `emergency_fund_months`: float (balance / monthly_expenses)

---

### Phase 3 — Cashflow Analyzer with 30/60/90-day Windows (Priority: High)

**What:** Time-windowed financial analysis that detects trends and anomalies.

**Files to create:**
- NEW: `app/ia/cashflow_analyzer.py`

**Outputs per time window:**
```python
{
  "window_days": 30,
  "total_inbound": 3800.00,
  "total_outbound": 2380.50,
  "net_cashflow": 1419.50,
  "avg_daily_outbound": 79.35,
  "savings_rate": 0.374,       # 37.4%
  "stability_index": 0.72,
  "emergency_fund_coverage": 3.2
}
```

**Rules engine (all deterministic):**
- If `savings_rate < 0.10`: flag LOW_SAVINGS_ALERT
- If `emergency_fund_coverage < 3`: flag LOW_EMERGENCY_BUFFER
- If net_cashflow negative 2 months in a row: flag NEGATIVE_CASHFLOW_TREND

---

### Phase 4 — Strategy Engine + Simulation Engine (Priority: Medium)

**What:** Generate a savings/investment strategy and project wealth growth.

**Files to create:**
- NEW: `app/ia/strategy_engine.py`
- NEW: `app/ia/simulation_engine.py`

**Strategy Engine output (deterministic rules):**
```python
{
  "monthly_savings_target": 900.00,
  "emergency_fund_target": 6,      # months
  "investment_suggestion": "ETFs + renda fixa",
  "priority": "emergency_fund_first"
}
```

**Simulation Engine (compound interest projection):**
```python
simulate_wealth_growth(monthly_investment=900.0, years=10, rate=0.10)
# -> { "year_5": 69_786, "year_10": 172_808, "year_20": 654_906 }
```

Formula: $FV = PMT \times \dfrac{(1+r)^n - 1}{r}$

---

### Phase 5 — Opportunity Engine (Priority: Medium)

**What:** Detect local/contextual financial opportunities based on user profile.

**Files to create:**
- NEW: `app/ia/opportunity_engine.py`

**Data sources (phase 1 = static rules, phase 2 = API integration):**
- User `address_city` from User model
- User savings_capacity from Phase 2
- Static opportunity catalog (JSON config): small business ideas per capital range

**Example output:**
```python
{
  "opportunity_type": "small_business",
  "title": "Maquininha de cafe ou vending machine",
  "location": "Campinas, SP",
  "startup_cost": 7000.00,
  "estimated_roi_months": 8,
  "fit_score": 0.82
}
```

---

## 4. API Surface (new endpoints)

| Method | Path                      | Description                                      |
|--------|---------------------------|--------------------------------------------------|
| GET    | `/ia/financial-snapshot`  | Full financial context snapshot (JSON)           |
| GET    | `/ia/wealth-score`        | Wealth score + breakdown                         |
| GET    | `/ia/cashflow`            | Cashflow analysis (30/60/90 day)                 |
| GET    | `/ia/strategy`            | Recommended savings + investment strategy        |
| GET    | `/ia/simulate`            | Wealth growth simulation (query params)          |
| GET    | `/ia/opportunities`       | Financial opportunity suggestions                |
| POST   | `/ia/chat`                | Existing — now receives financial context        |

---

## 5. Data Access Contract

The IA agent reads these tables (read-only):

| Table              | Fields read                                         | Purpose                  |
|--------------------|-----------------------------------------------------|--------------------------|
| `users`            | `balance`, `cpf_cnpj` (masked), `address_city`     | Profile + location       |
| `transacoes_pix`   | `value`, `type`, `status`, `created_at`, `fee_amount` | Transaction history    |
| `boleto_transactions` | `value`, `status`, `created_at`                  | Expense tracking         |
| `user_subscriptions` | `status`, `expires_at`                           | Subscription state       |

**The IA agent never writes. Zero INSERT/UPDATE from the AI layer.**

---

## 6. Security and LGPD Compliance

- CPF/CNPJ always masked before injection into LLM context
- Transaction descriptions filtered: no names, no keys, no addresses in LLM context
- All LLM calls logged with user_id + correlation_id (no PII in log body)
- Subscription gate: only active subscribers (`SubscriptionStatus.ACTIVE`) access the engine
- Rate limit: max 30 IA requests per user per hour (prevents abuse)

---

## 7. Stack Decisions

| Component               | Technology              | Rationale                              |
|-------------------------|-------------------------|----------------------------------------|
| Financial engine        | Pure Python functions   | Deterministic, testable, auditable     |
| LLM communication       | OpenRouter (gpt-4o-mini) | Existing integration, low cost        |
| Context serialization   | Pydantic v2 models      | Type-safe, validated, serializable     |
| DB queries              | SQLAlchemy ORM          | Parametrized, existing pattern         |
| Caching (future)        | Redis                   | Cache financial snapshots (5 min TTL)  |

---

## 8. Phase Delivery Timeline

| Phase | Deliverable                            | Scope                   |
|-------|----------------------------------------|-------------------------|
| 1     | Financial Context Injection            | IA aware of account     |
| 2     | Wealth Score Engine                    | Score + savings capacity |
| 3     | Cashflow Analyzer                      | 30/60/90d windows       |
| 4     | Strategy + Simulation Engine           | Recommendations         |
| 5     | Opportunity Engine                     | Local opportunities     |

---

## 9. Rejected Alternatives

| Alternative                    | Reason rejected                                              |
|--------------------------------|--------------------------------------------------------------|
| LLM reads DB via SQL tool      | Non-deterministic, injection risk, no audit trail           |
| Store financial state in Redis | Adds complexity without benefit at current scale            |
| Separate microservice          | Premature — monolith is valid at this stage of the product  |
| Third-party wealth API         | Cost, LGPD compliance issues, external dependency risk      |

---

## 10. Success Criteria

- Phase 1: IA responds with accurate balance and 30-day summary from real DB data
- Phase 2: Wealth Score reflects actual financial behavior (not just balance threshold)
- Phase 3: Cashflow alerts fire correctly on test data
- Phase 4: Simulation matches manual compound interest calculation (tolerance: 0.01%)
- All phases: `pytest` suite green, zero `get_errors` warnings, no PII in LLM context logs
