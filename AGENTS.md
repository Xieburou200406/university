# AGENTS.md — 波动率交易决策辅助系统 · Agent 开发指令

> 本文档是给 AI Agent（WorkBuddy / Claude / 其他编码智能体）的项目开发说明书。
> 任何 Agent 在此仓库工作时，**必须先完整读完本文档**，再动手写代码。
> 文档中的"铁律"均为本项目已经用真金白银的调试时间验证过的事实，违反任何一条都会重现历史上踩过的坑。

---

## 0. 一句话使命

**面向交易者的低频波动率交易决策辅助系统**：基于 50ETF 期权收盘截面数据，用 IV 分位数 + 历史情景类比双引擎生成交易建议，经风控条件链校验后展示给用户；**只输出建议，绝不自动下单**。当前处于"可用 Demo"阶段（M1–M4 已完成），你的任务是把它推向**正式版**（见 §8 里程碑）。

---

## 1. 项目当前状态（2026-10-08）

### 1.1 已完成（M1–M4，全部真实数据验证）

| 里程碑 | 内容 | 关键文件 | 实测结果 |
|---|---|---|---|
| M1 数据层 | AKShare 接口探针 + 上交所官方风险指标缓存管道 | `probe_akshare.py`, `run_demo.py` | 250→520 交易日全量缓存 |
| M2 定价/信号 | SVI 波动率曲线、IV 分位数 + 历史情景类比双引擎 | `pricing.py`, `signals.py`, `m2_run.py` | SVI R²=0.99；类比命中率 80.8% vs 基准 54.7%（360 样本） |
| M3 账本/归因/回测 | SQLite 快照差分四项归因、双引擎回测 | `m3_run.py`, `vol_demo.db` | 归因残差中位数 4%；口径校验 max 误差=0 |
| M4 面板/任务 | Streamlit 五 Tab 面板、每日定时任务脚本、周度复核 | `app.py`, `daily_job.py`, `weekly_review.py` | AppTest 冒烟 0 异常 |

### 1.2 文件地图

```
vol-demo/                 ← 本目录即 university.git 仓库根
├── probe_akshare.py      # AKShare 接口探针（接口变更时先改它）
├── pricing.py            # BS 定价 / 希腊字母 / SVI+poly3 曲线拟合
├── signals.py            # PercentileEngine（分位数状态机）/ AnalogEngine（k-NN 情景类比）
├── run_demo.py           # M1 端到端管线 → demo_report.html
├── m2_run.py             # M2：520 日历史 + SVI + 真实类比引擎 → m2_report.html
├── m3_run.py             # M3：SQLite 账本 + 归因 + 回测 → m3_report.html
├── app.py                # Streamlit 五 Tab 面板（localhost:8501）
├── daily_job.py          # 每日任务：拉数→信号→建议卡片→signal_log
├── weekly_review.py      # 周度复核：命中率 vs 基准，告警写 signal_log
├── data/cache/           # 官方风险指标日频缓存（ri_v2_YYYYMMDD.csv）
├── vol_demo.db           # SQLite：position / position_snapshot / signal_log
├── docs/                 # 架构设计与 Agent 文档（需求原始来源）
└── *.html                # 三份报告（每轮改代码后必须重新生成并核对数字）
```

### 1.3 已实测标定的接口事实（不许再猜）

- **数据源**：上交所官方每日风险指标 `ak.option_risk_indicator_sse(date="YYYYMMDD")`，一次返回全市场 ~580 条合约的 IV + Delta/Gamma/Vega/Theta。**历史 IV 序列不用自己算，拉官方的**；自算希腊字母只作交叉校验器。
- **合约字典**：`ak.option_current_day_sse()` 返回全部在市合约（含"到期日"列）。**注意：历史日期的已退市合约不在这个字典里**。
- **实时行情**：`ak.option_sse_spot_price_sina(symbol="合约代码")` **只支持逐合约查询**（逗号批量会失败），平值附近 ~22 个合约并行抓即可。
- **标的历史**：`ak.fund_etf_hist_em`（东财）易 ProxyError，必须重试 + `ak.fund_etf_hist_sina`（新浪）兜底；新浪返回的日期列是 `datetime.date` 类型，**必须先转字符串再比较**。
- **标的地实时价**：`ak.option_sse_underlying_spot_price_sina(symbol="sh510050")`，价格字段名叫 **"最近成交价"**（不叫"最新价"）。

---

## 2. 铁律（违反 = 重现历史事故，逐条有血泪出处）

1. **行权价量纲**：合约代码 `510050C2610M02700` 的后 4 位是行权价×1000，即 `02700 → 2.700 元`，**除以 1000，不是 10000**。错了会让 ATM 选取、ln(K/F)、偏离全错，且 R² 看起来还正常、极难察觉。（M2 事故：曾因此选到最高档冒充 ATM）
2. **官方希腊字母量纲**：`VEGA_VALUE` = **每 1.0 IV**（不是每 1%）；`THETA_VALUE` = **年化**（不是每日）。归因时 Vega 项 × ΔIV×100，Theta 项 ÷ 365。（M3 事故：残差占比曾高达 19649%）
3. **C/P 聚合**：同一行权价的 Call 和 Put 官方 IV 互相差异巨大（如 0.28 档一个 11.4% 一个 15.6%），拟合曲线前必须 `groupby("strike").mean()` 聚合，否则是过拟合噪声。
4. **到期日不能信缓存里的 `expiry` 字段**：它是 `202610` 这种年月标签，`pd.to_datetime` 会解析成 NaT。要么用合约字典的"到期日"列，要么从代码推（上交所 ETF 期权 = **到期月第 4 个周三**，月合约代码 `2610` → 2026 年 10 月）。
5. **禁止未来函数**：t 日特征只能用 t 日及以前的数据；t 日收盘算出的状态，**回测中最早 t+1 生效**。这是回测合法性的生命线。（M3 事故：状态当日生效赚当日涨跌 = 日内前视，修正后回测结果完全反转）
6. **持仓口径对齐预测期限**：类比引擎预测 10 日方向，回测/模拟就必须按 10 日持有期平滑，不能每日翻仓——否则方向命中率再高 PnL 也是亏的（曾 +80% 命中率回测亏 -5820，修正持有期后 +7360）。
7. **Fail-closed**：信号三条件（期望收益、胜率≥55%、P10 可承受）不满足就输出"观望"，**绝不硬选**；历史邻居 <10 个直接观望；流动性评分 <40 禁开新仓。
8. **T+1 执行时序**：T 日 15:30 出信号（参考价=T 日收盘中间价）→ T+1 9:35–10:00 执行窗口 → 未执行自动作废。回测成本必须含隔夜滑点 + 买卖价差全额 + 手续费（万 2 默认）。
9. **缓存版本化**：缓存文件名带版本号（如 `ri_v2_`）。**改了数据解析逻辑必须换版本号重新生成**，旧缓存不会自动失效，会让 bug 在缓存里复活。（M3 事故：行权价 bug 写进 v1 缓存，命中缓存不重算，污染了三份报告）
10. **单一真源**：全库希腊字母、IV、价格只有一种量纲口径，函数签名和注释写明；两个地方各算各的 = 口径漂移。
11. **只建议不下单**：系统任何模块不得对接真实交易接口。建议卡片必须带"仅供参考"标注。
12. **统一窗口**：IV 分位数窗口锁定 120 日（40 日窗口曾给出分位 1.0、120 日窗口同日 0.542——窗口选择直接决定信号方向，面板必须双窗口对照展示并注明正式口径为 120 日）。

---

## 3. 运行环境手册

### 3.1 Python 与依赖

- 托管 venv：`C:/Users/abigail/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（3.13，已装 akshare/pandas/numpy/scipy/plotly/streamlit）
- **禁止**全局 pip install，一切依赖进该 venv。
- akshare 版本 1.19.1 实测可用；AKShare 接口字段随版本漂移，**升级版本后必须先跑 `probe_akshare.py` 重探**。

### 3.2 运行命令

```bash
PY="C:/Users/abigail/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd <仓库根>

$PY run_demo.py        # M1 端到端 → demo_report.html（联网拉当日截面）
$PY m2_run.py          # M2 → m2_report.html（读本地缓存，秒级；缺的天数自动补拉）
$PY m3_run.py          # M3 → m3_report.html + vol_demo.db
$PY weekly_review.py   # 周度复核 → data/review_log.csv 追加
$PY daily_job.py       # 每日任务（注册 schtasks 见文件头注释）
$PY -m streamlit run app.py --server.port 8501 --server.headless true   # 面板
```

### 3.3 网络与代理（这台机器的特俗性，必读）

- git 访问 GitHub 必须走用户本地代理 **7890**，且要**清掉环境变量里的 http_proxy**（沙箱代理 49899/59244 对 GitHub 返回 502）：

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
```

- 首次克隆：`git clone -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 https://github.com/Xieburou200406/university.git`
- AKShare 数据源（上交所/东财/新浪）**不走** 7890 也能通，不要给数据请求挂代理。

---

## 4. 架构分层与数据流

```
[数据层] 唯一允许联网的层
  probe_akshare / run_demo.step1-4 / m2_run.load_day
     │  官方风险指标(IV+希腊字母) + 标的价 + 合约字典
     ▼  落地：data/cache/ri_v2_YYYYMMDD.csv（列: code,strike,cp,iv,delta,gamma,vega,theta,expiry）
[定价层] 纯函数，不联网
  pricing.py: bs_price/greeks/brent_iv/_fit_svi/fit_curve_iv
     │  SVI 拟合（IV尺度残差+多起点重启+无蝶式套利约束），失败自动降级 poly3
     ▼
[信号层] 可插拔引擎，不联网
  signals.py: PercentileEngine(120日分位+连续2日确认状态机)
              AnalogEngine(6维特征 k-NN 检索→9策略池→加权统计μ/胜率/P10-P90→三条件选策略)
              ensemble 模式 = 两引擎同向才出信号
     ▼ signal_log (SQLite)
[风控层] 条件链逐级收窄，任何一条不过就削减/拒绝
  Vega上限 → Delta上限 → 单strike仓位 → 流动性闸门(≥40) → 深虚深实禁开 → 资金占用 → 信号一致性
     ▼
[组合层] m3_run: 模拟持仓(SQLite) → 每日快照差分四项归因(Delta/Vega/Theta/残差) → 回测
     ▼
[展示层] app.py 五Tab（IV监控/信号台/风控台/归因/回测）——只读，"刷新数据"按钮显式联网
```

**分层纪律**：信号模块禁止出现任何实盘/回测分支（保证两口径一致）；展示层禁止计算（只读数据库与报告）；定价层禁止 import akshare。

---

## 5. 数据契约（改这些 = 全链路联改）

### 5.1 缓存 CSV 列（`ri_v2_YYYYMMDD.csv`）

| 列 | 含义 | 坑 |
|---|---|---|
| `code` | 合约代码如 `510050C2610M02700` | 行权价=末4位/1000；到期月=第6-9位推第4个周三 |
| `strike` | 行权价（已除 1000 的正确量纲） | v1 缓存是错的，勿用 |
| `cp` | C / P | 同 strike 需聚合 |
| `iv` | 官方隐波（小数，0.273=27.3%） | 深实深虚=0，须过滤 `iv>0` |
| `delta/gamma/vega/theta` | 官方希腊字母 | vega 每1.0 IV；theta 年化 |
| `expiry` | `202610` 年月标签 | **不是日期**！解析到期日用 `expiry_from_code()` |

### 5.2 SQLite 表（vol_demo.db）

- `position(code, qty, open_date, open_px, …)` — 模拟持仓账本
- `position_snapshot(date, mv, cum_pnl, acc_delta, acc_vega, acc_theta, spot, legs_json)` — 每日快照（归因的输入）
- `signal_log(date, engine, signal, confidence, detail_json)` — 信号流水 + 周度复核告警也写这里

### 5.3 信号 JSON（引擎 → 风控的接口）

```json
{
  "date": "2026-10-08", "engine": "analog|percentile|ensemble",
  "signal": "SHORT_VOL|LONG_VOL|NEUTRAL|WAIT", "confidence": 0.84,
  "iv_now": 0.273, "iv_pct": 0.542, "window": 120,
  "valid_until": "2026-10-09",          // T+1 作废
  "candidates": [{"code": "...", "action": "SELL", "deviation_bp": 230, "liq": 78}],
  "analog_detail": {"k": 20, "mu": ..., "win_rate": ..., "p10": ..., "p90": ...}
}
```

---

## 6. 编码规范

- Python，标准库 + pandas/numpy/scipy/plotly/streamlit/akshare，**不引入重型框架**（fastapi/django 一律不要）。
- 每个函数 docstring 第一行写清**量纲**（"iv 为小数"、"vega 每 1.0 IV"、"返回年化"）。
- 所有联网调用必须 try/except + 打日志 + 有兜底通道或跳过该日（数据层断一天不能让全链路崩）。
- 聚合并行抓取用 `ThreadPoolExecutor(max_workers=8)`，逐请求 sleep ≥0.03s 防封。
- 报告 HTML 由 plotly 生成，**每轮改动后必须重新生成三份报告并 grep 核对关键数字**（这是本项目的"测试"）。
- 中文注释，报告面向散户读者，专业指标必须带一句人话解释。

---

## 7. Agent 工作流（你该怎么做这件事）

1. **动手前**：读本文件 §1–§5；`git status` 确认干净；跑一遍 `m2_run.py` 确认缓存可读、基线数字与 §1.1 一致（SVI R²≈0.99，命中率≈80%）。
2. **改数据逻辑必换缓存版本号**（铁律 9），并重跑 M2/M3 全链路。
3. **每完成一个功能**：重新生成受影响的报告 → grep 核对关键数字没有意外漂移 → `git add -A && git commit`（信息用中文、说清动机）→ 用 §3.3 的代理命令 push。
4. **遇到数字异常**：先查 §2 铁律清单逐条排除，再怀疑新代码。历史上 80% 的"模型问题"最后都是量纲/口径/缓存问题。
5. **AKShare 报错/接口变更**：改 `probe_akshare.py` 重探签名，不要凭记忆猜字段名。
6. **不确定需求细节**：向用户确认 §8 里程碑的优先级，而不是自己发明需求。

---

## 8. 里程碑：从 Demo 到正式版（按此顺序推进）

### M5 工程化重构（当前优先级最高）
- [ ] 把散装脚本重构成包：`vol/` 包 + `data/` `pricing/` `signals/` `risk/` `portfolio/` `report/` 子模块；脚本只剩薄入口。
- [ ] 配置外置 `config.yaml`：窗口 120、K=20、风控上限、费率、执行窗口、路径——代码里不许有魔法数字。
- [ ] `requirements.txt` + 一键安装说明；`README.md`（面向用户：这是什么、怎么跑、怎么读面板）。
- [ ] 关键纯函数（bs_price/greeks/svi/percentile/atm选取/行权价解析）补 `tests/` 单元测试——**用 §2 铁律当测试用例**（例：`parse_symbol("510050C2610M02700")["strike"] == 2.7`）。

### M6 数据与信号强化
- [ ] 每日定时任务真正注册运行（schtasks / 用户常开机器的方案），signal_log 持续积累。
- [ ] 类比引擎特征扩充：期限结构斜率、VRP（IV−已实现波动率）、20 日动量（设计文档 §5.2b 已列，尚未全实现）。
- [ ] 命中率周度复核自动化运行 + 面板展示复核历史曲线；跌破基准连续 4 周 → 面板顶部黄色警告。

### M7 组合与风控完整化
- [ ] 用户可在面板添加/删除模拟持仓（现在只有代码里写死的示例持仓）。
- [ ] 风控条件链 7 条全部实装并逐条展示通过/削减/拒绝状态（当前只实装了 Vega/Delta 两条）。
- [ ] 流动性评分：买卖价差 + 挂单量合成 0–100 分（实时行情接口已探明，逐合约并行抓）。
- [ ] 合约档位标注（平值/虚值/实值 ± 档位数）+ 推荐开仓载体筛选。

### M8 交付打磨
- [ ] 回测加隔夜滑点模型（T 收盘中间价 vs T+1 开盘价）。
- [ ] 面板加"声明页"：只输出建议不构成投资建议、信号 T+1 有效、类比引擎为统计非保证。
- [ ] 打包：PyInstaller 单机版或 Docker；可选发布为在线链接（WorkBuddy sites）。
- [ ] 历史扩到全量（2015 年起），复核类比引擎在 2020/2024 极端行情的表现并写进报告。

**验收标准（每条里程碑通用的 Done 定义）**：三份报告重新生成且关键数字无意外漂移；新增纯函数有单测且通过；AppTest 冒烟 0 异常；报告/面板上的数字能对上数据库原始行；已 commit + push 到 GitHub。

---

## 9. 可直接粘贴的 Agent 主提示词

> 用户可以把下面这段原样发给任何 AI Agent 来启动本项目工作。

```
你是本项目的量化开发 Agent。工作目录：本仓库根（vol-demo，即 university.git）。

【使命】把"低频波动率交易决策辅助系统"从可用 Demo 升级为正式版：基于上交所官方
风险指标数据，用 IV 分位数 + 历史情景类比双引擎生成 50ETF 期权交易建议，经风控
条件链校验后通过 Streamlit 面板展示。系统只输出建议，绝不自动下单。

【必读】开工前完整阅读仓库根的 AGENTS.md，特别是 §2 铁律（12 条实测标定的坑，
违反任何一条都会重现历史事故）和 §8 里程碑清单。

【当前任务】从 M5 工程化重构开始：①把散装脚本重构成 vol/ 包；②配置外置
config.yaml；③补 requirements.txt 和 README；④给关键纯函数写单元测试（用
铁律里的量纲案例当测试用例）。每完成一项：重新生成报告核对数字、commit、push。

【环境】Python 用 C:/Users/abigail/.workbuddy/binaries/python/envs/default/
Scripts/python.exe。git 走代理：env -u http_proxy -u https_proxy -u HTTP_PROXY
-u HTTPS_PROXY git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=
http://127.0.0.1:7890 push origin main。

【纪律】改数据解析逻辑必须换缓存版本号（ri_v2→ri_v3）重跑全链路；禁止未来函数
（t 日状态最早 t+1 生效）；风控不满足就输出观望（fail-closed）；所有函数 docstring
标注量纲；每轮改动后报告关键数字必须与数据库原始行对得上。
```

---

## 10. 历史事故台账（新 Agent 必读的"前人坑"）

| # | 事故 | 症状 | 根因 | 对应铁律 |
|---|---|---|---|---|
| 1 | ATM 选取全是最高档 | 曲线 R² 0.82 但 x 轴全错 | 行权价除以 10000 | 1 |
| 2 | 归因残差 19649% | 四项拆解完全失真 | vega/theta 量纲理解错 | 2 |
| 3 | SVI 拟合出平线 R²=0.07 | 曲线无形态 | 总方差尺度拟合 + 未聚合 C/P | 3 |
| 4 | 250 天只有 109 天有效 | 历史大面积丢失 | 历史合约不在今日字典，映射不到到期日 | 4 |
| 5 | 类比命中率虚高 86.7% | 后续验证崩到 60% | 错误 ATM 序列写进缓存并存活 | 1+9 |
| 6 | 回测 +80% 命中率却亏钱 | 方向对但 PnL 负 | 日内前视 + 每日翻仓不对齐 10 日预测期 | 5+6 |
| 7 | 同日分位数 1.0 vs 0.542 | 信号方向打架 | 窗口选择不同 | 12 |
| 8 | 面板缓存 pickle 报错 | AppTest 异常 | st.cache_data 缓存了 SVI 闭包 | —（缓存只存可序列化数据） |
| 9 | 批量删缓存被拦 | 删除失败 | safe-delete 钩子 >50 文件需确认 | —（用版本号弃用，不硬删） |

---

*本文档由 WorkBuddy Agent 于 2026-10-08 基于 M1–M4 全部实测结果撰写。项目需求原始来源见 `docs/波动率决策辅助系统-架构设计与Agent文档.md`。*
