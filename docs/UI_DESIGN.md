# YugiTalk 当前 UI 规范

更新日期：2026 年 11 月 13 日。实际界面由 `frontend/src` 构建。阶段 0 的橙黑静态草图保存在 `archive/ui/`，仅供历史设计和相应测试查看，不再作为当前视觉规范。

## 视觉与布局

当前采用蓝白知识工作台：浅色画布、白色主区、淡蓝选中态、轻量边框和底部输入框；保留深色主题。主题变量与覆盖样式位于 `frontend/src/shared/hermes.css`，在 `main.tsx` 中于基础 `styles.css` 之后加载。浅色主要变量为背景 `#ffffff`、画布 `#f5f8ff`、主文字 `#182744`、强调色 `#2467f6`，深色使用独立变量。

左侧展示新建对话、功能导航和历史，右侧按当前视图显示内容；手机使用折叠导航。聊天、知识库、Agent、工具、Skill、任务、监控和管理员操作各自维护状态，不能将管理员可见控件当成权限校验。

## 对话交互

- 输入区固定在聊天区域底部，支持附件、助手和 Skill 选择。
- Enter 发送，Shift + Enter 换行，中文输入法组合期间不误发送。
- 活动任务支持独立的暂停、继续和停止；插话默认面向最近任务，也可指定目标或发送新任务。
- 澄清卡片展示系统问题与候选选项，对应 waiting_input 原 Run。
- 处理过程只展示公开阶段与工具状态；同名阶段在简略视图去重，完整记录仍保留在后端。
- 来源、交付文件和聊天附件独立展示，不由模型生成任意下载路径。

交互逻辑见 `chat/ChatView.tsx`、`ChatTask.tsx`、`ProcessSummary.tsx`，HITL 行为见 [HITL](HITL.md)。

## 页面专题

[Agent 配置](AGENT_WORKSPACE_UI.md)、[Skill MCP Tool 配置](CONFIGURATION_WORKSPACES_UI.md)、[知识库](KNOWLEDGE_LIBRARY_UI.md)、[任务与对话菜单](TASK_HISTORY_UI.md) 分别描述专题页面，公共视觉以本文件和实际样式为准。

## 验证与历史记录

本次插话交互已有 Edge 隔离页面回归，覆盖目标选择、暂停时提交、网络失败重试、另开任务及 360 像素布局。该测试使用模拟 API，不代替真实模型问答。

早期 Hermes 改版保留的桌面、深色和手机截图位于 `stages/artifacts/hermes-chat-desktop.png`、`hermes-chat-dark.png`、`hermes-chat-mobile.png`，只代表当时版本。阶段 0 原始草图和 axe 测试结果见 [阶段报告](stages/00-design-and-probes.md)。静态草图发送禁用，不调用业务 API；当前工作台可以实际运行任务，两者不可混用验收。
