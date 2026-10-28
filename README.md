# 土档 · 国有土地租赁智能管理

Python 3.12 + FastAPI + SQLite + 文件证据仓库 + 原生 JavaScript。界面不依赖 CDN，单 worker。
需求依据：《国有土地租赁智能管理_赛题需求说明》（下称“赛题”）、练习集《06_计租标准与核算口径》（下称“口径”）、《使用说明.md》。

## 赛题一期建设内容与实现位置

| 赛题表2 | 实现 | 入口 |
|---|---|---|
| 1 存量合同治理 | 导入（图片/PDF/DOCX/XLSX、ZIP 整包）→ 识别候选 → 待核验工作台 → 逐项核对或批量确认 → 入库；问题清单按口径第十一条 13 类自动判定 | 01 材料与任务、02 待核验工作台、03 证据复核 |
| 2 一宗一档电子台账 | 宗地按登记单元号 / 地号（新旧格式互认）/ 原合同编号链归集；可按街路、合同状态、欠缴、关键字查询 | 04 宗地台账、`GET /api/ledger?street=&status=&arrears=&keyword=` |
| 3 租金核算 | `app/domain.py` 规则程序逐年分段；计租标准版本化可配置，改了规则对历史期间重算并留差异 | `GET/POST /api/rules*` |
| 4 到期与欠缴提醒 | 红/橙/黄分级；可导出 `.ics` 导入日历 | `GET /api/reminders`、`?format=ics` |
| 5 催缴通知 | 按 07_模板 生成通知书 DOCX 与 ≤70 字短信；登记审核/发送/缴清等结果形成催缴记录 | `POST /api/notices/{台账版本}`、`/api/notices/{id}/events` |
| 6 合同生成与续期变更 | 按制式模板生成新签/续期合同草稿并归档；变更协议可导入并归宗；解除/收回填停止计租日 | `POST /api/contracts/draft` |
| 7 税务推送单 | 按税务字段生成，每期留存可追溯 | `POST /api/tax-push`、`GET /api/tax-pushes` |
| 8 统计查询 | 欠缴、到期、收缴率（累计/当年）、分街路统计，可导出 | `GET /api/stats`、`?format=csv` |

人工智能只做识别、抽取、比对和提示；**金额只由规则程序计算**；所有模型结果为“待确认”，经人确认后才入库（赛题五(3)）。

## 目录与业务基础数据

程序需要练习集（或评测集、正式导出）目录中的 `02`–`05` 四张表和 `07_模板`。查找顺序：

1. 环境变量 `FDE_SAMPLES_DIR`（评测集、正式数据都用它切换）；
2. 代码目录的上一级：`../练习集`、`../*/练习集`（如 `../国有土地租赁模拟样本包_练习集/练习集`）、`./练习集`。

```text
黑客松/
├── 代码/                                   # 本仓库
└── 国有土地租赁模拟样本包_练习集/练习集/    # 自动找到；评测集请用 FDE_SAMPLES_DIR 指定
```

## 安装与启动

联网开发机（Python 3.12）：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.in
cp .env.example .env              # 需要云模型时再填密钥
./scripts/start.sh                # http://127.0.0.1:8765
```

Windows 开发机：`py -3.12 -m venv .venv`，`.venv\Scripts\python -m pip install -r requirements.in`，`powershell -File scripts\start.ps1`。

**服务器部署（多名经办人员访问）**：必须启用本地账号并声明浏览器访问地址，否则 `start.sh` 拒绝监听非本机地址：

```sh
.venv/bin/python scripts/manage.py add-user config/users.json 张三 --role reviewer
.venv/bin/python scripts/manage.py add-user config/users.json 管理员 --role admin
FDE_HOST=0.0.0.0 FDE_PORT=8765 FDE_USERS_FILE=$PWD/config/users.json \
FDE_ALLOWED_ORIGINS=http://10.1.2.3:8765 FDE_SAMPLES_DIR=/data/正式导出 ./scripts/start.sh
```

`FDE_ALLOWED_ORIGINS` 是浏览器地址栏里的地址（逗号分隔多个），用于拒绝跨站写入。离线安装包：联网构建机运行
`python3.12 scripts/build_offline.py --output /abs/new/dir`，目标机 `FDE_PYTHON=python3.12 ./scripts/install.sh`
（只用包内 wheel，`--no-index --require-hashes`；Python 运行时本身未打包，需目标机预装）。

## 模型配置与现场切换

`FDE_PROVIDERS_CONFIG` 指向一份配置即切换（不改代码、不重启以外的操作）：

| 配置 | 用途 |
|---|---|
| `config/providers.competition.json` | 比赛阶段云端视觉模型。`authorized_scope: competition_simulated` 允许评测集与现场补档（均为模拟材料）外发；**正式环境禁用** |
| `config/providers.local.json` | 政务网/本机：`local_rapidocr` 本机开源 OCR（不联网），以及内网 vLLM/Ollama 等 OpenAI 兼容服务（`http://10.x` 内网地址允许 HTTP、可不配密钥） |
| `config/providers.siliconflow.json` 等 | 原有单供应商配置 |

外发规则：公网模型只接收已授权哈希或 `competition_simulated` 范围的材料；内网/本机模型不受此限（数据不出网）。
本机 OCR 需额外安装 `requirements-local-ocr.in`。

**练习集实测（本机开源 OCR）**：`python3 scripts/measure_local_ocr.py`。RapidOCR（PP-OCRv4 ONNX，CPU）+ 条款句式抽取，
50 份 3.4 秒/份，关键字段唯一且正确率：签订日期 84%、宗地号 80%、计租方式 48%、起止日期约 40%、面积 30%、
承租人 22%、坐落 0%；**没有一份关键字段全对**。结论：轻量本机 OCR 只能做辅助与离线兜底，全部送人工复核；
批量确认门槛仍需视觉大模型实测校准；供应商连通记录见下方鉴权修正，连通成功不等于准确率通过。

批量确认要求原件引用可验证且无阻断告警，并须逐份人工核对原件、资料完整性、付款及核算。
同名弱匹配付款只列候选、不预选，须逐项复核；同一票据不能重复计入不同有效宗地。
新签/续期草稿按同一组面积、用途、级别和起始日适用标准计算初始年租金，缺标准或方式未定时拒绝生成。

## 核算口径要点（`app/domain.py`）

- 年度计费月数含起止当月；年度应缴 = 适用年租金 × 月数 ÷ 12，每份合同分别取整后相加。
- 随基准地价调整：面积 × 当年 1 月 1 日有效标准；合同未载明级别时按登记坐落（无登记则合同坐落）所在街路定级。
- 大小写不一致按大写；期满未续按末份合同方式计至基准日所在年度全年；解除/收回计至当月。
- 缴费：票据号去重；注明年度只记该年度（超出为多缴、不冲抵他年）；未注明年度冲抵最早未缴足年度并依次向后。
  缴费日期晚于基准日的缴款计入实缴并注明。欠缴 = 应缴之和 − 实缴之和（含多缴）。
- 存疑事项（口径第十二条）由程序列出各算法并取应缴最少者：计租方式未勾选、涂改处无签章、两份合同期间重叠、
  同票据号金额不一致；“存疑待确认”写明暂按算法与相差金额，导出时单列《待确认清单》。
- **取整口径待确认**：口径写“四舍五入到分”（默认 `half_up`）。练习集标准答案实际用二进制浮点 `round()`，
  在恰好 x.xx5 时会舍去（如 1999.125→1999.12），5 宗地因此差 0.01–0.07 元。`FDE_ROUNDING=answer_key`
  可复现标准答案用于自测；正式口径须主管部门书面确认。

## 测试

界面选择器由 `app/static/controls.js` 在页面内统一渲染，保留原生 `select` 的值、禁用状态和 `input/change` 事件，避免桌面 Chrome 与内置浏览器调用不同的系统菜单。新增动态选择器、更新选项或通过代码修改 `value/disabled` 后，调用 `SelectControls.enhance()` 同步显示；不要从可见文案反推业务值。下拉支持方向键、Home/End、Enter、Escape、Tab 和菜单滚动，视口变化时重定位。

```sh
.venv/bin/pip install pytest ruff
.venv/bin/ruff check app tests scripts/manage.py scripts/build_offline.py
.venv/bin/pytest -q
node --check app/static/app.js
```

`tests/test_alignment.py` 用练习集标准答案的已确认要素走与 API 相同的归宗 → 缴费候选 → 核算 → 问题清单流程，
该夹具假定人工已接受所有练习集匹配付款（包含同名弱匹配），并固定使用评分标签“自动通过”，不代表实际批量分流结果。
再调用出题方 `score.py`：口径取整 96.2 分，`answer_key` 取整 100 分（问题清单 F1 100%、存疑待确认 3/3 文字与金额一致）。
这验证的是规则引擎；识别准确率另由 `measure_local_ocr.py`/云端实测给出。
也可手动：`python3 tests/exercise_pipeline.py <练习集>/08_标准答案 out/ && python3 <练习集>/score.py <练习集>/08_标准答案 out/`
（需在仓库根目录、`PYTHONPATH=.`）。

## 日常运维

- 备份：界面“运行证据→备份”或 `.venv/bin/python scripts/manage.py backup /abs/new/dir`
  （默认目录 `FDE_BACKUP_DIR`，否则数据目录旁的 `*-backups/`）。内容：SQLite 在线快照 + 原件 + 识别证据 + SHA-256 清单。
- 恢复：`scripts/manage.py restore <备份目录> <新目录>`，校验通过后 `FDE_DATA_DIR=<新目录> ./scripts/start.sh`。不覆盖旧库。
- 日志：uvicorn 标准输出；业务操作与 AI 结果、人工修改、确认、撤回、发文、推送均在审计表（触发器禁止改删）。
- 账户、API 密钥、`config/notice.json` 不在业务备份内，由运维单独保管。

## 尚未完成 / 需说明的边界

- 视觉大模型在练习集上的实测、批量确认比例与准确率、3,000 份人员与天数测算（依赖真实识别结果）。
- SOP、经办人员操作手册、年度运行费用测算等交付文档不在本仓库。
- 短信通道、催缴文号与缴费地点、税务“本期应缴”口径、取整方式待主管部门确认（赛题第八节）。
- PDF 内含多份文书时不会自动拆分；ZIP 分组错误可在复核页“并入所选文书”修正。

## 审查文档

- [PR #1 修复与复验](docs/PR1修复与复验.md)：四个验收缺陷、主线整合及本轮测试证据。

- [需求对齐与代码审查报告](docs/需求对齐与代码审查报告.md)：22 处缺陷逐条、赛题表 2 对齐矩阵、验证输出，以及待主管部门拍板的口径问题（取整方式、短信通道、税务“本期应缴”）。

### 2026-10-28 云端鉴权修正

现有凭据值保持原样，按官方案例调整绑定和协议：

- 阿里读取 `AI_ALIYUN_KEY`，使用新加坡 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`；同一 Key 北京返回 401、新加坡模型列表与 `qwen-vl-ocr-2025-11-20` 图片识别均返回 200。参考[官方地域地址](https://help.aliyun.com/en/model-studio/base-url)与[OCR 示例](https://help.aliyun.com/zh/model-studio/qwen-vl-ocr-api-reference)。
- 百度现有 `AI_BAIDU_KEY` / `AI_BAIDU_SECRET` 是可用于 AI OCR 的应用凭据：OAuth 换 Token → 表单提交 PaddleOCR-VL 文档解析任务 → 5 秒间隔查询 → 获取 BOS JSON 结果。Token 仅驻留内存；查询超时保留 task_id，不自动重提。参考[官方请求案例](https://ai.baidu.com/ai-doc/OCR/3mi73at9o)。千帆 V2 Bearer 是另一种协议，只应使用独立的 `AI_BAIDU_QIANFAN_API_KEY`。

验证记录：阿里 `runs/cloud-live-20261028T073841Z-aliyun/`；百度 `runs/cloud-live-20261028T073637Z-baidu/`。均使用 LX-017_1 模拟样张，证明调用成功，不代表字段准确率验收。百度未返回实际权重版本，适配器不推断版本、不伪造文字坐标，候选仍须人工核验。两个提供方配置保留按需启用及样张 hash 限制。
