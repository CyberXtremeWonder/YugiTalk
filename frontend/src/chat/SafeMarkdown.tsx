import type { ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** 模型生成的链接仅展示文字，防止诱导点击将上下文带到外部站点。 */
function InertLink({ children }: { children?: ReactNode }) {
  return <span title="对话中的链接仅展示文字">{children}</span>;
}

/** 不创建图片元素，从渲染源头阻止外链图片和查询参数自动发送上下文。 */
function BlockedImage({ alt }: { alt?: string }) {
  return (
    <span className="blocked-image">[图片已屏蔽{alt ? `：${alt}` : ""}]</span>
  );
}

/** 流式与历史回复共用安全渲染：不解析 HTML，不加载图片，链接不可执行。 */
export function SafeMarkdown({ children }: { children: string }) {
  return (
    <Markdown
      skipHtml
      remarkPlugins={[remarkGfm]}
      components={{ a: InertLink, img: BlockedImage }}
    >
      {children}
    </Markdown>
  );
}
