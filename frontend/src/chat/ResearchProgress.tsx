import { useEffect, useState } from "react";
import { identityRequest } from "../identity/api";
import "./attachments.css";

interface Progress {
  title: string;
  current_step?: string;
  plan?: { questions: { title: string; criterion: string }[] };
}

/** 当前对话仅在实际调用研究工具后显示计划，离开会话停止轮询。 */
export function ResearchProgress({ runId }: { runId: string }) {
  const [items, setItems] = useState<Progress[]>([]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    /** 串行读取公开进度，终态或权限失效后停止。 */
    async function refresh() {
      try {
        const result = await identityRequest<{
          items: Progress[];
          status: string;
        }>(`/research/runs/${runId}/progress`);
        if (!active) return;
        setItems(result.items);
        if (["queued", "running"].includes(result.status)) {
          timer = setTimeout(() => void refresh(), 2000);
        }
      } catch {
        if (active) setItems([]);
      }
    }
    void refresh();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [runId]);
  return (
    <>
      {items.map((item, index) => (
        <details className="research-progress" key={index} open>
          <summary>{item.current_step || "正在研究并生成文件"}</summary>
          {item.plan && (
            <ol>
              {item.plan.questions.map((question, i) => (
                <li key={i}>
                  {question.title}：{question.criterion}
                </li>
              ))}
            </ol>
          )}
        </details>
      ))}
    </>
  );
}
