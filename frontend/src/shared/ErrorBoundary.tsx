import { Component, type ReactNode } from "react";
import { reportRuntimeFailure } from "./monitoring";

/** 捕获所有业务页面的渲染异常，并提供恢复入口。 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  /** 将渲染失败转换为可恢复的提示页面。 */
  static getDerivedStateFromError() {
    return { failed: true };
  }

  /** 记录安全错误类别，不采集页面内容或组件参数。 */
  componentDidCatch(error: Error) {
    reportRuntimeFailure("react.render", error);
  }

  /** 正常展示业务页面，异常时允许用户重新加载。 */
  render() {
    if (this.state.failed)
      return (
        <main role="alert">
          <p>页面遇到错误，请重新加载后重试。</p>
          <button onClick={() => window.location.reload()}>重新加载</button>
        </main>
      );
    return this.props.children;
  }
}
