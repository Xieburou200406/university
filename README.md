# 低频波动率交易决策辅助系统

面向个人期权交易者的**低频**（T 日收盘信号 → T+1 执行）波动率决策辅助系统。数据源为上交所 ETF 期权（510050 等），**只生成建议，绝不自动下单**。

> ⚠️ 免责声明：本系统输出仅为研究性建议，不构成投资建议；模拟盘成交假设偏乐观，模拟盈利不构成实盘盈利预期。

## 功能总览

| 模块 | 说明 |
|---|---|
| 每日体检 | 近月/次月 ATM IV、25Δ 偏斜、期限斜率、VRP、20 日动量，落 `daily_metric` |
| 信号引擎 | A. IV 分位状态机（120 日窗口 + 2 日确认）；B. 类比引擎（k-NN 历史相似情形，非均值回归）；C. ensemble 合成 |
| 隔夜预测 | T+1 开盘方向 P(up)：逻辑回归 + 概率校准，双时点（T 收盘版 / 9:25 竞价复核版） |
| 风险预算 | 今日最优 Vega/Delta 敞口：类比情景集 + 均值-方差效用，硬限额夹逼 |
| 风控条件链 | Vega/Delta 限额、流动性分、单行权价集中度、信号一致性，全程留 trace |
| 建议+执行 | 建议卡片（有效期 T+1）、挂单价三档、持仓两源对账、执行质量回顾 |
| 归因 | 每日快照差分：Delta/Vega/Theta/残差四项拆解，残差 <7% |
| 回测 | 分位 vs 类比 vs 买入持有长波；状态滞后 1 日消灭前视 |
| 模拟盘（设计） | 回放时钟 + 虚拟撮合 + 多账户 + 策略模板 + 跟建议 vs 自由对比 + 错单本 |

## 快速开始

```bash
# 环境（Windows，隔离 venv 已含依赖）
pip install -r backend/requirements.txt

# 1) 生成 520 交易日缓存并跑管线（首次需联网，AKShare）
python m2_run.py
python m3_run.py

# 2) 启动 API（交互文档 http://127.0.0.1:8000/docs）
python run_api.py

# 3) 启动 Streamlit 面板（http://localhost:8501）
streamlit run app.py --server.port 8501

# 4) 数据库迁移（正式库 data/vol.db）
cd backend && alembic upgrade head && cd ..

# 5) 测试（27 个，全部离线）
python -m pytest backend/tests -q
```

每日定时（交易日 15:30）可用 Windows 任务计划调用 `daily_job.py`。

## 架构

```
vol-demo/
├─ backend/
│  ├─ vol/            # 领域包（唯一真源）：contracts/units/config/pricing/signals
│  ├─ db/             # SQLAlchemy 2.0 模型（9 表）+ Alembic 迁移 + 会话工厂
│  ├─ api/            # FastAPI 壳层：routers/schemas/services（M5-④）
│  ├─ config.yaml     # 全部魔法数字（window=120, vega_limit=881, ...）
│  └─ tests/          # 27 个单测（离线，内存库）
├─ data/vol.db        # 正式库（WAL）；data/cache/*.csv 为逐日截面缓存
├─ m2_run / m3_run    # 管线（调 vol 包；m3 双写正式库）
├─ app.py             # Streamlit 面板（读正式库 data/vol.db）
├─ run_api.py         # API 启动器
├─ docs/详细设计-后端与前端.md   # §1-§20 详细设计（§20=行业常情审计）
└─ docs/参考学习-QS知识库.md     # QS 参考知识库学习报告（日志/规则载入/文档规范）
```

依赖方向：`api → services → vol.* / db`，web 层永不进入领域层。

## 铁律（防呆内建，不靠自觉）

1. 行权价 ÷1000（02700 = 2.700）
2. VEGA = 每 1.0 IV 口径；THETA = 年化
3. 波动率曲线拟合强制 C/P 聚合
4. 到期月标签 ≠ 真实到期日（第 4 个周三）
5. 禁未来函数（信号窗口截至昨日）
6. 缓存版本化（改解析逻辑必换 `cache_version`）
7. fail-closed（任何一步失败即不出结论）
8. T+1 执行（信号次日有效，过期作废）
9. 单一数据源（领域包唯一真源）
10. 只建议，不下单
11. IV 分位窗口正式口径 120 日
12. 归因用持仓期对齐（快照差分，禁累计值直接拆解）

## 验证数字（安全网，回归必查）

- SVI 拟合 R² = 0.9899
- 类比引擎方向命中率 74.55%（520 日 80.8%）vs 基准 53.21%
- 归因残差 7%（520 日 4%），归因校验最大差 = 0
- 回测（修正后）：类比 +7360 vs 买入持有长波 +10

## 文档

- `AGENTS.md` — Agent 协作规约（里程碑 M1-M8）
- `docs/详细设计-后端与前端.md` — 架构、DB、API、前端、调度、执行建议、模拟盘、隔夜预测、风险预算、UX 与系统设计修订、日志运维升级、交易所规则载入引擎、建议生命周期、行业常情审计（§1-§20）
- `docs/参考学习-QS知识库.md` — QS 参考知识库的学习报告：日志管理（明细/聚合两级、快照差分、排查 runbook、daily/curr 轮转）、交易所规则载入（规则库+交易日历，已落地 backend/vol/calendar）、其他工程规范（公式三件套、金样本、形状恒等式校验）
- `波动率决策辅助系统-架构设计与Agent文档.md` — 早期架构文档
