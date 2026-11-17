import { useState } from "react";

/** 明确区分错误和普通反馈，不通过文字内容猜测提示类型。 */
export function useNotice() {
  const [feedback, setFeedback] = useState({ message: "", error: false });
  return {
    notice: feedback.message,
    noticeIsError: feedback.error,
    setNotice: (message: string) => setFeedback({ message, error: false }),
    reportError: (message: string) => setFeedback({ message, error: true }),
  };
}
