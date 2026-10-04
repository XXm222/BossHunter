---
name: BossHunter 招聘界面（第一套）
description: 已确认的蓝白招聘工作台与岗位额度页面设计记录
colors:
  primary: "#2464e9"
  primary-hover: "#1c54cf"
  ink: "#192e4e"
  muted: "#536883"
  surface: "#fff"
  line: "#e0e8f2"
  tint: "#edf4ff"
  sidebar: "#f0f6fc"
  nav-active: "#dceaff"
  nav-active-text: "#1e61db"
  table-heading: "#f0f5fb"
  button-line: "#cfdcf0"
  button-text: "#225dbf"
  button-hover: "#f0f6ff"
  disabled-bg: "#eaf0f8"
  disabled-text: "#768aa5"
  badge-bg: "#eef2f7"
  badge-text: "#5f7189"
  warning-bg: "#fff2dd"
  warning-text: "#95570e"
typography:
  headline:
    fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "23px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "-0.45px"
  title:
    fontSize: "16px"
    fontWeight: 650
    lineHeight: 1.4
  body:
    fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "13px"
    lineHeight: 1.6
  label:
    fontSize: "12px"
    fontWeight: 550
    lineHeight: 1.5
  metric:
    fontSize: "26px"
    fontWeight: 650
    lineHeight: 1.25
rounded:
  badge: "4px"
  control: "5px"
  panel: "7px"
  circle: "50%"
spacing:
  compact: "7px"
  controls: "12px"
  grid-small: "14px"
  panel: "18px"
  spacious: "20px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.button-text}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-disabled:
    backgroundColor: "{colors.disabled-bg}"
    textColor: "{colors.disabled-text}"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
  search:
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "37px"
  badge:
    backgroundColor: "{colors.badge-bg}"
    textColor: "{colors.badge-text}"
    rounded: "{rounded.badge}"
    padding: "3px 7px"
  nav-active:
    backgroundColor: "{colors.nav-active}"
    textColor: "{colors.nav-active-text}"
    rounded: "{rounded.panel}"
    padding: "10px 12px"
---

# Design System: BossHunter 招聘界面（第一套）

## Overview

采用用户已确认的第一套明亮蓝白操作界面：浅蓝侧栏、白色内容面板、克制边框、多层彩色 SVG 和紧凑业务表格。视觉重点服务于岗位选择、处理进度和明确的下一步。

本次页面正文范围为招聘工作台（`/recruiting`）、岗位与每日额度（`/recruiting/positions`）、主动打招呼（`/recruiting/discover`）及独立候选人沟通页（`/recruiting/candidates`、`/recruiting/candidates/:id`），另有共享导航、页头和图标。候选人沟通以已批准的 `../../outputs/招聘UI第一套/找人与候选人沟通-待确认.png` 下半屏为视觉依据。其他页面正文仍保留原设计；此文档不表示全系统改版完成。参考图属于视觉依据，其示例数字不是实际数据。

**Key Characteristics:**

- 浅蓝导航与白色业务面板形成层次。
- 彩色图标使用独立分层 SVG，文字与数字保持清晰。
- 状态、空数据和未接通能力均用真实文案呈现。

## Colors

蓝色用于主要动作、选中状态和额度进度；深蓝文字配合灰蓝辅助说明，浅蓝背景承担区域分层。警示标签使用暖色，使执行阻塞容易识别。具体复用色值以 frontmatter 为准。

## Typography

页面与侧栏采用 Inter、苹方、微软雅黑及无衬线回退。页标题、面板标题、正文、操作标签和指标数字按 frontmatter 区分层级；业务表格主要使用 12px，次级信息使用 10–11px。指标和额度数字使用等宽数字特性。600px 以下页标题为 21px、指标为 23px。

## Layout

桌面侧栏宽 192px，页头高 58px；内容最大宽度 1500px。工作台为主列加 315px 状态侧栏，岗位页面为主列加 294px 额度侧栏，间距分别为 18px、20px。1600px 起两页侧栏分别增至 340px、320px。

1199px 以下收紧间距；岗位页在 601–1199px 改为单主列，额度和提示在其下方两列排列。工作台在 980px 以下转为单主列。767px 以下共享侧栏缩至 68px，保留图标和短标签。600px 以下标题区纵向排列、侧面板纵向堆叠；岗位表将城市、薪资等合并进岗位主列，仍保留选择和详情。工作台表隐藏今日招呼列。表格容器支持自身横向滚动。页面内容设置相对定位，令隐藏表单标签的绝对定位保持在主内容滚动范围，避免窄屏产生多余页面空白滚动；主内容使用浅蓝灰滚动条。

主动打招呼页面依次排列横向额度带、已选岗位表、触达记录与详情。桌面记录主列配 320px 详情列，间距 18px；1250px 以下详情列收至 275px、间距 14px，额度带隐藏节奏说明。1050px 以下记录与详情上下排列，额度带的剩余额度另起一行。600px 以下额度带改单列，岗位表隐藏今日招呼数与本轮进展；记录表保留时间、候选人/岗位、结果和详情操作，长文本允许换行，底部阻塞说明换行排列。

候选人沟通采用左筛选列表、中对话与回复框、右助手/上下文/简历/面试三列。桌面列宽为 `240px minmax(300px,1fr) 340px`；1250px 以下为 `200px minmax(265px,1fr) 300px`；701–1100px 保留左列表，聊天和助手在右列上下排列；700px 以下切换独立列表、对话、详情视图。各列内容独立滚动。

## Elevation & Depth


层次主要来自浅色背景、细边框与内容间距。主要按钮使用轻微投影（`0 2px 4px #2464e918`），开关圆点使用微小投影（`0 1px 2px #26354c20`）；普通面板无额外阴影。完整焦点、投影和动效记录在 sidecar。

## Shapes

面板和导航使用柔和小圆角，控件与标签更紧凑；计数、头像和状态点使用圆形。表格通过行分隔线组织信息，多层彩色 SVG 保持原有叠层、明暗与不同色块，不能简化成单色描边图标。

## Components

- **按钮与输入：** 主要按钮最小高度 37px；保存动作依据忙碌与修改状态禁用。键盘焦点有清晰描边；搜索框用整体 focus-within 提示。按钮背景过渡为 0.15s。
- **导航：** `/recruiting/discover` 在侧栏及页头统一命名“主动打招呼”。当前项使用浅蓝底、蓝字和左侧短标记，悬停略加深底色。侧栏导航区域独立滚动。过渡为 0.16s，减少动态效果偏好下关闭。
- **工作台：** 统计、待处理候选人、岗位预览和 Agent 状态均由业务状态生成；没有已选岗位时显示开放岗位预览并明确提示。准备动作导航到配置，运行中提供停止监测动作。
- **岗位列表：** 开放/其他岗位标签支持方向键及 Home/End；按名称、城市和关键词筛选。开放岗位支持勾选与开关，两者编辑同一份草稿；保存后才生效。有未保存选择时禁用同步。详情在表格内展开。
- **额度：** 所有勾选岗位共用账号每日额度；平台额度模式与自定义上限二选一。保存设置不启动外发。额度总量未知用破折号，剩余未知显示“未读取”；自定义进度按尝试数计算，成功数单独显示。
- **主动打招呼额度带：** 复用白面板、操作蓝与浅蓝进度条；已确认数和尝试数分开表达。平台总额度未知时不呈现确定进度，也不把“未读取”解释为零。开始执行按钮在无阻塞时可用、执行中变为停止、有阻塞时禁用，并关联底部说明。
- **岗位执行与记录：** 只显示已保存选择的岗位；未选择时引导至岗位与额度。记录来自当天北京时间的只读尝试数据，支持候选人/岗位搜索、全部/已招呼/已跳过/待核实筛选、每页 6 条及上一页/下一页。筛选按钮使用 `aria-pressed` 和蓝色下边线；选中记录使用浅蓝行背景。只有 `sent` 计入已确认，其他未确认结果不呈现为成功；无数据和筛选无结果分别显示文案。
- **触达详情：** 展示所选记录的岗位、结果与北京时间。仅凭候选人标识精确匹配现有会话，匹配后才提供该候选人的沟通入口；未关联资料显示明确占位说明。历史记录未保存联系依据时如实说明，不推测匹配理由。候选人对话、简历与评估继续在独立沟通页面查看。
- **边界：** 主动打招呼执行与平台剩余额度读取已接通（真机验收）；本地 Cookie 的只读同步入口存在，不代表执行链路已连通。不得操作用户 Chrome 标签页。

## Do's and Don'ts

候选人沟通只读自动打开首位候选人，保留全部已加载消息并初始定位最新；切换候选人清空回复框。采用建议只填入编辑框，手机同步切回对话并聚焦；保存明确为草稿。真实资料缺失如实显示，不填演示分数；面试邀约发送保持禁用，既有待核实状态保留。占位文字使用 `#536883`，白底对比度 5.71:1。本轮真实快照为 1 个会话、7 条消息和部分简历；9 张真实截图与手机采用建议 fixture 分开保存。三尺寸只读 QA 无 POST；38 项前端测试及构建通过，不代表外发验收，本次未改后端。

- Do 使用实际业务数据，并保留未读取、待接通、未保存等状态。
- Do 保留键盘焦点、语义化控件和减少动态效果设置。
- Do 延续分层彩色图标及紧凑表格，适配窄屏时把岗位信息合并进主列。
- Don't 将参考图里的示例数字作为业务数据。
- Don't 把保存岗位或额度设置呈现为已经启动自动外发。
- Don't 将本次已批准页面的设计范围解释为其他页面正文已经迁移。
