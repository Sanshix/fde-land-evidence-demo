# 土档 · 国有土地租赁证据工作台

Python 3.12 + FastAPI + SQLite + 文件证据仓库 + 原生 JavaScript。完整本机业务服务，界面不依赖 CDN。默认只监听 `127.0.0.1:8765`，单 worker。

**当前云端未取得成功响应。硅基流动和阿里已实际发起连接测试，均连接超时；百度已更正Key并实测，同样连接超时。** 无配置会进入人工旁路，不下载或调用本地模型。测试中的 `test_stub` 只验证接口与业务门禁。历史 `runs/paddle-probe.json` 是早先本机探针，不能当作当前云方案验收。

## 从源码仓库准备环境

仓库仅包含 `demo` 系统源码、测试、脚本与非秘密配置；不包含 `.env`、本机账户配置、数据库、上传原件、运行记录、虚拟环境、本地模型或目录外的练习集。

**当前程序启动依赖仓库同级的 `练习集` 目录。只克隆源码尚不能直接启动。** 请另行从原项目资料取得下列文件，保持目录结构；仓库不附带标准答案：

```text
工作目录/
├── fde-land-evidence-demo/       # 本仓库
└── 练习集/
    ├── 02_不动产登记导出.xlsx
    ├── 03_租金缴费记录.xlsx
    ├── 04_租金减免登记.xlsx
    ├── 05_计租标准与土地级别.xlsx
    └── 07_模板/
        ├── 国有土地租金催缴通知书（模板）.docx
        └── 税务推送单（字段与样式）.xlsx
```

前四张表是启动必需数据，模板用于导出；样张导入、云端样张探针及相关测试另需 `练习集/01_合同扫描件`。离线构建脚本也依赖原练习集中的业务基础文件。请按原资料访问权限单独提供这些文件。

联网开发机安装 Python 3.12 后，在仓库根目录运行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.in
cp .env.example .env
# 按需在 .env 填写密钥，并依下文配置云模型；人工旁路无需密钥。
./scripts/start.sh
```

运行测试与静态检查时另需安装 `pytest` 和 `ruff`。`scripts/install.sh` 面向已经构建好的离线交付包，需要 `wheelhouse` 和 `requirements.lock`；源码克隆使用上面的联网安装步骤。

## 本机启动

当前开发环境已有 `.venv`：

```sh
./scripts/start.sh
# 浏览器 http://127.0.0.1:8765
# 健康检查 http://127.0.0.1:8765/healthz
```

默认 `loopback_demo` 允许界面选择演示角色，不等于账号认证。要验证真实权限，请自行指定本地用户密码：

```sh
.venv/bin/python scripts/manage.py add-user config/users.json reviewer --role reviewer
.venv/bin/python scripts/manage.py add-user config/users.json admin --role admin
FDE_USERS_FILE="$PWD/config/users.json" ./scripts/start.sh
```

浏览器会要求 HTTP Basic 登录；服务端忽略伪造角色头，分别强制只读/经办/管理员权限。仅用于回环地址或已有可信 TLS 的内网入口；未提供生产 SSO。账户文件不会进入安装包和业务备份，需由运维单独保管。

## 云模型接入（需用户明确目的地与数据范围）

将 `config/providers.example.json` 复制为 `config/providers.json`，按供应商填写 `base_url`、确切 `model_id`、`provider`、指定密钥变量名（项目密钥按域名绑定，测试可用显式 `FDE_*` 环境变量）和每条路径的 `authorized`；最后明确启用 `enabled`。**不要将密钥写进 JSON 或聊天**。程序仅读取本项目允许名单中的 `.env` 变量或显式环境变量，不发现其他应用账号。`credentials.py`把各平台变量绑定到对应官方域名，阻止跨平台误用。

支持 OpenAI-compatible结构化视觉chat、专业OCR纯文本，以及百度千帆专用OCR协议；三者协议均有标注为test_stub的本地测试，实际供应商响应尚未验证。`config/providers.siliconflow.json`、`providers.aliyun.json`、`providers.baidu.json`保存非秘密配置，可用`FDE_PROVIDERS_CONFIG`选择，当前均禁用以免网络故障下反复调用。百度/阿里仅允许已批准样张hash。`primary`→`secondary`→`third`→`context` 按缺失、格式、图像质量、证据缺失和分歧升级。第三/上下文未配置会明确跳过；同一模型不能计作独立第二路径。局部可定位时裁剪升级，无定位时整页。上下文返回值仍是建议，不自动确认。

开源要求需核对官方模型卡、权重许可、版本/修订号与供应商模型身份，记录 `open_weights/model_card_url/license/weight_revision/identity_verified`。填写这些元数据不会自动获得“开源已实测通过”。至少一个真实测试模型满足该条件后，才算完成此项。

`fictional_exercise` 仅允许原始练习图片 SHA-256 匹配；任意上传合同不会自动获准外发。其他输入要在授权后显式列入 `authorized_sha256`。未授权时零外发、转人工。云请求失败不无限重试；再次操作可复用已完成页/模型的证据，标记 `resumed_evidence`。页预算默认60秒，文书180秒，批次20分钟；预算检查不等于已经证明整包达标。

## 多格式数据治理

支持图片、原生/扫描/混合PDF、DOCX、XLSX；原件hash和全部页/段落/表格/单元格清单保留。Excel原值、显示解释、日期epoch、公式/缓存分别存；隐藏内容和合并范围可见；不执行宏、公式、嵌入对象。旧DOC/XLS及宏格式只存证并明确失败。原生结构提取不是AI准确率结果。

独立“待核验工作台”集中识别问题、原件问题、业务冲突、未完成和待确认。原件先显示，候选默认折叠；人工采纳需引用真实位置、写依据、处理阻断项并核对完整性。补件使旧草稿、确认与预览失效；无法辨认则挂起。

## 核心业务

- 原件内容哈希去重、文书页序/旋转、持久任务、有限识别路径、逐页证据候选、人工改值/引用页/依据、无法辨认挂起。
- 所有 AI 结果均待人工确认；关键事实未核实不能入有效台账。确认前先预览核算及新旧差异；版本冲突阻断发布。
- 稳定内部宗地 ID；精确地籍映射，缴费/减免由人工选定；不以姓名直接归宗。
- Decimal 确定性计算，逐年分段与付款归集、票据去重、大小写金额规则、到期未续继续计租。规则来源：练习集05、06。
- 单事务发布、撤回某批贡献、重导复用证据后再次确认；发布/通知/费率/审计历史由数据库触发器禁止修改删除。
- 管理员费率表版本：`GET /api/rules` → `POST /api/rules/preview` → `POST /api/rules/confirm`，提交差异摘要哈希、来源理由和本次核算复核声明；全体有效宗地重算，新版发布、旧版通知冻结。
- ZIP 补档支持按文件夹分文书或根目录 `LX-017_1.jpg` 页码约定。API `/api/import-batch`，返回逐份结果；计时和内容正确性须分别验收，挂起不冒充全部完成。
- 生成基于模板的催缴 DOCX 和短信草稿，**从未实际发送**。缺发文参数、金额大写复核和可选段审核，因此明确水印草稿。
- 税务 XLSX 为模拟交换草稿；季度“本期应缴”口径不明时留空，不能擅自年租金除4。没有税务接口调用。

## 三端架构与后置打包

用户已确认完整业务优先。核心采用跨平台Python、SQLite、相对路径/可配置 `FDE_DATA_DIR`，模型provider、账户与秘密分离；Windows/Linux/macOS各需对应Python运行时和wheel。以下构建脚本是前期探针，当前不继续打三端包。`交付包/fde-macos-arm64-20261028-v3`早于当前schema5及新版界面，不是最新版交付。

## 安装与可复现打包

联网构建机（已有 Python3.12 和 pip）运行：

```sh
python3.12 scripts/build_offline.py --output /absolute/new/fde-package
```

只下载公开软件依赖 wheel，不下载模型。包含业务源代码、精确版本/哈希依赖、10份声明过的开发样本、业务基础表、模板、安装启动脚本；不含真值、密钥、生产数据、历史本地模型。

目标机**预装 Python3.12 + venv**后：

```sh
cd /path/to/fde-package/demo
FDE_PYTHON=/path/to/python3.12 ./scripts/install.sh
./scripts/start.sh
```

安装器仅 `--no-index --find-links=wheelhouse --require-hashes`，不会下载组件。构建包只适配构建时 OS/架构；不是跨平台包。**Python运行时/操作系统未打进包，不可声称满足空白服务器全离线交付；须锁定比赛服务器后制作相应运行时/镜像并在干净 VM 验收。** 云推理联网与业务软件离线安装是两项独立条件。

## 备份恢复

```sh
.venv/bin/python scripts/manage.py backup /absolute/new/backup-dir
.venv/bin/python scripts/manage.py restore /path/to/backup /absolute/new/restore-dir
FDE_DATA_DIR=/absolute/new/restore-dir ./scripts/start.sh
```

SQLite 在线快照 + 原件 + 识别运行证据 + SHA-256 清单。恢复只接受新目录，校验文件/DB完整性、重定位原件路径、保留业务快照与审计；排队/运行任务转为中断，需显式重试。不要覆盖旧库。账户和 API 密钥不在业务备份中。备份目录含合同内容，按原数据权限保存。

## 验证与当前边界

界面选择器由 `app/static/controls.js` 在页面内统一渲染，保留原生 `select` 的值、禁用状态和 `input/change` 事件，避免桌面 Chrome 与内置浏览器调用不同的系统菜单。新增动态选择器、更新选项或通过代码修改 `value/disabled` 后，调用 `SelectControls.enhance()` 同步显示；不要从可见文案反推业务值。下拉支持方向键、Home/End、Enter、Escape、Tab 和菜单滚动，视口变化时重定位。

```sh
.venv/bin/ruff check app tests scripts/manage.py scripts/build_offline.py
.venv/bin/pytest -q
node --check app/static/app.js
```

测试使用 synthetic/test_stub 标记的数据。开发10份文书与其余40份留出集分离，不向模型输入标准答案或 scorer 输出。

尚未通过：真实云识别和独立第二模型；开源模型身份核实及练习集实测；全50文书/30宗地逐宗逐年核对；陌生整包20分钟；真实人工耗时；完整13类问题自动发现；全部补档文书类型/争议多情景暂算；正式SSO/TLS；干净目标服务器与离线运行时安装。规则修订目前支持费率表，通用计算政策变更需代码版本。当前界面记录的复核停留时间包含闲置，不当作真实人工净工时。已发布材料修订当前需先撤回；尚未实现旧有效版本与新草稿并存的完整版本树。


### 2026-10-28 云端鉴权修正

现有凭据值保持原样，按官方案例调整绑定和协议：

- 阿里读取 `AI_ALIYUN_KEY`，使用新加坡 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`；同一 Key 北京返回 401、新加坡模型列表与 `qwen-vl-ocr-2025-11-20` 图片识别均返回 200。参考[官方地域地址](https://help.aliyun.com/en/model-studio/base-url)与[OCR 示例](https://help.aliyun.com/zh/model-studio/qwen-vl-ocr-api-reference)。
- 百度现有 `AI_BAIDU_KEY` / `AI_BAIDU_SECRET` 是可用于 AI OCR 的应用凭据：OAuth 换 Token → 表单提交 PaddleOCR-VL 文档解析任务 → 5 秒间隔查询 → 获取 BOS JSON 结果。Token 仅驻留内存；查询超时保留 task_id，不自动重提。参考[官方请求案例](https://ai.baidu.com/ai-doc/OCR/3mi73at9o)。千帆 V2 Bearer 是另一种协议，只应使用独立的 `AI_BAIDU_QIANFAN_API_KEY`。

验证记录：阿里 `runs/cloud-live-20261028T073841Z-aliyun/`；百度 `runs/cloud-live-20261028T073637Z-baidu/`。均使用 LX-017_1 模拟样张，证明调用成功，不代表字段准确率验收。百度未返回实际权重版本，适配器不推断版本、不伪造文字坐标，候选仍须人工核验。两个提供方配置保留按需启用及样张 hash 限制。
