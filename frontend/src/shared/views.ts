export const views = {
  trace: {
    title: "Trace",
    symbol: "≋",
    color: "blue",
    description: "任务执行明细与真实模型用量。",
    empty: "",
    hint: "",
  },
  monitoring: {
    title: "实时监控",
    symbol: "▥",
    color: "blue",
    description: "运行用量与异常监控。",
    empty: "",
    hint: "",
  },
  chat: {
    title: "对话",
    symbol: "◉",
    color: "purple",
    description: "",
    empty: "",
    hint: "",
  },
  agents: {
    title: "Agent",
    symbol: "✳",
    color: "orange",
    description: "为不同任务，选择合适的智能助手。",
    empty: "尚未创建 Agent",
    hint: "模型、工具与提示词配置将在后续阶段提供。",
  },
  skills: {
    title: "Skill",
    symbol: "◇",
    color: "purple",
    description: "保存常用流程，为任务选择合适的技能。",
    empty: "尚无自定义 Skill",
    hint: "创建技能后可在对话中选择。",
  },
  tools: {
    title: "Tool",
    symbol: "⚒",
    color: "blue",
    description: "管理工具目录与调用权限。",
    empty: "尚无自定义工具",
    hint: "内置天气、路况和文本工具，可按需配置。",
  },
  knowledge: {
    title: "知识库",
    symbol: "▤",
    color: "green",
    description: "把你的资料，变成有据可循的答案。",
    empty: "知识库还是空的",
    hint: "文档上传与索引将在后续阶段提供。",
  },
  tasks: {
    title: "任务记录",
    symbol: "✓",
    color: "blue",
    description: "执行过程与结果，在这里清晰呈现。",
    empty: "尚无任务记录",
    hint: "查看任务执行、结果与恢复状态。",
  },
  settings: {
    title: "设置",
    symbol: "⚙",
    color: "orange",
    description: "管理你的个人偏好与记忆。",
    empty: "服务连接状态",
    hint: "当前仅验证基础服务，尚未连接模型。",
  },
  development: {
    title: "管理员面板",
    symbol: "⌘",
    color: "blue",
    description: "开发模式的服务、账号与审计管理。",
    empty: "",
    hint: "",
  },
} as const;
export type ViewId = keyof typeof views;
/** 从地址栏读取受支持的页面标识，未知值回到默认对话页。 */
export function readView(): ViewId {
  const value = window.location.hash.slice(1);
  return Object.hasOwn(views, value) ? (value as ViewId) : "chat";
}
export const prompts = [
  {
    title: "知识检索",
    symbol: "▤",
    color: "purple",
    text: "检索我的项目资料，整理关键结论。",
  },
  {
    title: "工具调用",
    symbol: "⚒",
    color: "green",
    text: "帮我选择合适的工具完成任务。",
  },
  {
    title: "任务计划",
    symbol: "✳",
    color: "orange",
    text: "帮我把这个想法拆解成执行计划。",
  },
  {
    title: "偏好记忆",
    symbol: "◆",
    color: "yellow",
    text: "整理我的工作偏好，并等待我确认保存。",
  },
  {
    title: "报告生成",
    symbol: "✓",
    color: "blue",
    text: "根据提供的资料，生成一份结构清晰的报告。",
  },
] as const;
