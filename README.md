# 碳排放核算与交易管理系统

面向控排企业的碳管理平台：活动数据采集、排放核算、配额分配、配额交易台账、履约清缴与年度 MRV 报告。

## 技术栈

- **后端**：Python 3.10+ / FastAPI / SQLAlchemy ORM / SQLite / JWT（Cookie 认证）
- **前端**：React 18（本地 UMD 运行时 + htm 模板引擎，无需构建工具，完全离线可用）
- **测试**：pytest（38 项全部通过）

## 快速开始

```bash
pip install -r requirements.txt
python scripts/init_db.py      # 初始化数据库与演示数据
uvicorn app.main:app --reload  # 启动服务
```

访问 http://127.0.0.1:8000

### 演示账号（密码均为 `123456`）

| 用户名    | 角色       | 说明                       |
|-----------|------------|----------------------------|
| `admin`   | 监管管理员 | 企业/因子/配额/清缴全权限   |
| `verifier`| 核查员     | 核验活动数据、批准 MRV 报告 |
| `elec`    | 控排企业   | 绿能电力集团（边界受限）    |
| `cement`  | 控排企业   | 恒固水泥股份（边界受限）    |

## 功能模块

1. **核算边界管理**：企业注册行业/地区/核算边界说明，范围一/二/三边界配置
2. **排放因子库**：因子编号、有效期、数据来源；修订自动记录版本历史
3. **活动数据台账**：企业按年度/周期录入活动量，核查员核验标记
4. **排放核算引擎**：
   - `activity_factor`：排放量 = 活动量 × 因子值
   - `fuel_combustion`：排放量 = 燃料量 × 综合系数 × 碳氧化率 × 44/12
   - 因子按年度生效区间取值，重复核算幂等（先清后算）
5. **配额管理**：免费配额分配（基准 + 分配量 + 调整量）、配额账户余额
6. **配额交易台账**：买入/卖出/划转，实时校验可用余额，逐笔记录余额快照
7. **履约清缴**：按核查排放量划转配额，配额不足自动记为缺口（deficit）
8. **MRV 报告**：年度范围一二三汇总生成，草稿 → 提交 → 批准状态流转

## 并发一致性保证

配额划转与履约清缴统一经 `app/services/ledger.py` 处理，避免超额扣减、重复履约与账实不符：

- **账户锁定**：SQLite 每个写事务以 `BEGIN IMMEDIATE` 立即取写锁（串行化，配合 `busy_timeout`）；PostgreSQL/MySQL 使用 `SELECT ... FOR UPDATE`，多账户按 id 升序加锁防死锁
- **原子条件扣减**：余额校验下推为 `UPDATE ... SET balance = balance - :amt WHERE balance >= :amt`，以影响行数判定成败，杜绝读-判-写之间的 TOCTOU 双花
- **幂等提交**：交易流水携带账户级唯一的 `request_id`，客户端双击/网络重试复用首笔流水；`(企业, 年度)` 唯一约束 + 保存点兜底，重复分配、重复清缴只生效一次；清缴完成后再次调用直接返回原履约记录
- **失败回滚**：所有台账写操作走统一事务上下文，异常一律回滚；`quotas`/`allowance_accounts`/`compliance_records` 均有唯一约束，外键开启杜绝 `account_id=0` 类脏流水

## 数据表（12 张）

`users` `companies` `emission_scopes` `activity_data` `emission_factors` `factor_versions` `calculation_methods` `emission_results` `quotas` `allowance_accounts` `allowance_transactions` `compliance_records` `mrv_reports`

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（Cookie 会话） |
| GET | `/api/dashboard/stats` | 平台统计 |
| GET/POST | `/api/companies` | 企业列表/创建（admin） |
| POST | `/api/companies/{id}/scopes` | 添加核算边界（admin） |
| GET | `/api/companies/{id}/totals?year=` | 年度范围汇总 |
| GET/POST | `/api/activity` | 活动数据列表/录入 |
| POST | `/api/activity/{id}/verify` | 核验（verifier/admin） |
| GET/POST | `/api/factors` | 因子列表/创建（admin） |
| PUT | `/api/factors/{id}` | 修订因子并记版本（admin） |
| POST | `/api/companies/{id}/calculate?year=` | 触发核算 |
| GET | `/api/companies/{id}/results?year=` | 核算明细 |
| GET/POST | `/api/quotas` | 配额列表/分配（admin） |
| POST | `/api/accounts/{id}/transfer` | 配额交易 |
| POST | `/api/companies/{id}/clear` | 履约清缴（admin） |
| GET | `/api/compliance` | 履约记录 |
| POST | `/api/companies/{id}/reports/generate` | 生成 MRV 报告 |
| POST | `/api/reports/{id}/submit` / `/approve` | 提交/批准报告 |

## 测试

```bash
python -m pytest tests/ -v   # 38 passed
```

覆盖：核算引擎两种公式、因子按年取值、核算幂等、配额分配幂等、清缴达标/缺口、交易余额校验、MRV 状态机、API 冒烟，以及并发卖出/清缴/分配的原子性、`request_id` 幂等、失败回滚与账实一致（`tests/test_concurrency.py`）。
