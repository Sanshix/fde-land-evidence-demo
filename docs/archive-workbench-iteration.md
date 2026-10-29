# 专业档案工作台迭代（2026-10-28）

本轮依据用户提供的《土地租赁项目工作台前端优化方案》，修改 `app/static/index.html`、`app/static/style.css`、`app/static/app.js`。保留原生 HTML/CSS/JavaScript、现有五个视图和 FastAPI 协议，没有新增前端构建链。

## 实现

- 视觉：按纸白 `#F5F6F3`、深墨 `#183247`、印章红 `#A43B32` 统一语义色、字体、边界与留白。重整 CSS 为共享组件、页面布局和单一断点规则；移除侧栏渐变和多轮覆盖。状态统计改成紧凑状态条。
- 材料队列：名称/编号搜索、状态过滤、最近/最早更新与名称排序，每页 20 份；增加更新时间和版本。视图、文书、查询、排序、页码写入 URL hash 并恢复。首次读取显示占位，读取失败可原地重试；轮询保持正在编辑的输入和已聚焦列表行。
- 待办分流：逐项复核区与批量区分离，显示问题分类计数。人工区只突出主要问题，其余展开查看。批量区仅列符合条件的材料，沿用服务端最多 200 份的单批上限；确认声明与操作集中，保留逐份部分失败结果。
- 证据核对：26 字段按文书与主体、宗地信息、租期与租金、原件质量分组，字段键和提交顺序不变。四阶段导航、可展开工作区、当前字段标记、当前字段候选、原件定位与文本播报、原件及 OCR 异常提示。日期/数值使用对应输入语义；不规范的既有值退回文本输入，以免浏览器控件静默丢弃原文。
- 台账：增加合同—登记对照，金额优先呈现；逐年、分段、缴费归集和审计数据展开查看，负欠缴解释为净多缴/抵减。快照未包含的独立登记用途、完整租期明确说明，未补造数据。
- 交互：统一可访问 dialog 处理登记、批量登记、合并、补件、调整原件、撤回、规则发布；续期改为起止日期表单并校验先后。催缴状态可填写并保存处理意见。对话框支持 Escape、原生焦点限制和返回焦点。
- 权限：角色、文书状态、业务条件共同控制写入口；涵盖合并、续期、催缴状态、税务、批量确认及复核字段。无欠缴按钮不会因权限刷新重新启用。
- 稳健性：45 秒请求超时覆盖响应体读取，超时不自动重试写请求；明确提示服务器可能已经接收。409 保留输入并给出重新核对说明。补件与主上传支持格式一致。
- 可访问性：跳至主内容、表格 caption/scope、具名且可聚焦的滚动区、带文书名/位置的原件替代文字、现场操作持久错误、减少动态效果及强制色彩模式。

`version`、`draft_hash`、身份请求头、四项确认声明、未保存保护与原生 select 的 value / input / change 契约保留。所有金额直接展示服务端值，不在浏览器重算租金。

## 验证方式

浏览器测试只连接独立临时数据目录，设置 `FDE_DISABLE_WORKER=1`，未写业务库、未调用云模型。合成图片及模拟接口结果均标为测试材料。

```sh
.venv/bin/pytest -q
node --check app/static/app.js
node --check app/static/controls.js
git diff --check

# 独立终端启动临时实例；端口必须同时配置，以符合现有同源校验。
FDE_DATA_DIR=$(mktemp -d /tmp/fde-ui-test.XXXXXX) \
FDE_DISABLE_WORKER=1 FDE_PORT=8879 \
FDE_ALLOWED_ORIGINS=http://127.0.0.1:8879 \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8879

# 在同一个 playwright-cli 会话依序执行：
playwright-cli -s=archive-ui open http://127.0.0.1:8879
playwright-cli -s=archive-ui run-code --filename=tests/workbench.browser.js
playwright-cli -s=archive-ui run-code --filename=tests/review-presentation.browser.js
playwright-cli -s=archive-ui run-code --filename=tests/archive-workbench.browser.js
# 无障碍测试使用本机 /tmp/fde-a11y/node_modules/axe-core/axe.min.js（4.14.0），不进入应用构建。
playwright-cli -s=archive-ui run-code --filename=tests/archive-accessibility.browser.js
```

实际 API 流程与模拟故障分开记录：上传/草稿/预览/登记、合并、待补件、续期草稿、催缴意见、撤回使用真实临时 API；503/409/超时/批量部分失败和无欠缴状态使用测试路由。模拟结果不代表识别或财务正确率验收。

## 本次验证结果

- Python：111 项测试通过。
- JavaScript 语法检查与 `git diff --check` 通过。
- 原有工作台流程、复核展示及新增交互回归通过，没有未捕获的 JavaScript 异常。首次加载失败与原地重试另行注入 503 验证通过。
- 五个视图分别检查 320、768、1024、1440px，无页面级横向溢出；核对页在 1050px 边界以下切为单栏。原有复核展示另覆盖 1366px。
- axe-core 4.14.0 的 WCAG 2 A/AA 与 2.1 AA 标签扫描：五个视图均为 0 个确定 violations；仍有 incomplete 项需要人工判断，详见下节。
- 已实测键盘下拉、对话框 Escape/返回焦点、跳至主内容，以及减少动态效果和强制色彩媒体模式。未将模拟媒体或扫描结果等同于真实读屏认证。

## 验收边界

自动检查与截图不能替代真实读屏。浏览器对原生缩放快捷键未产生可验证的视口变化，因此不把响应式调整或 CSS 放大计为“原生 200% 缩放通过”。真实读屏、原生 200% 缩放、真实经办效率计时仍待人工验收。

axe 对隐藏共享下拉浮层的 `aria-controls`、纯符号刷新图标会留下需人工判断的 incomplete 项；这是已有菜单结构的显式引用，不等于已完成读屏认证。保留原生菜单契约，不为清空扫描信息重写控件。

本轮不包含服务端分页、审批体系、OCR 准确率、生产部署、安装包重建。预览截图和日志保存在 `output/playwright/archive-*`；测试实例停止后，这些文件仍可用于复核。
