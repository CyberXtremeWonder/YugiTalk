# Hermes 风格前端调整

参考用户提供的截图重做视觉层：白色主界面、窄侧栏、淡蓝选中态、轻量顶部栏、细边框消息与底部输入区。保留中文、浅色默认、深色切换以及原有 Agent、Skill、Tool、知识库、任务和管理入口。截图中的宣传字幕与背景照片未加入工作台。

样式入口为 `frontend/src/shared/hermes.css`，通过 `main.tsx` 在基础样式后加载。只调整外观，没有复制外部项目代码或新增网络依赖。

验证：类型检查、ESLint、前端构建通过；隔离浏览器覆盖注册、管理员模式、工具审核、桌面聊天、浅深主题与390px手机布局，无横向溢出、页面异常或外部请求。截图保存于 `docs/stages/artifacts/hermes-chat-desktop.png`、`hermes-chat-dark.png`、`hermes-chat-mobile.png`。未执行真实模型问答。

开发前端镜像 `yugitalk-frontend:0.1.0` 已重新构建，没有重启现有容器。按启动手册选择当前环境启动，然后浏览器 Ctrl+F5 刷新。正式生产镜像0.2.0需按生产发布流程构建，不能因此视为已发布。
