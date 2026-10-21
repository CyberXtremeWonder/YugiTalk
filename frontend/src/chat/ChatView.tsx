import { Sources } from "../rag/Sources";
import { Fragment, useEffect, useRef, useState } from "react";
import { SafeMarkdown } from "./SafeMarkdown";
import { withoutSourceLabels } from "../rag/presentation";
import { prompts } from "../shared/views";
import { allItems, identityRequest, type Identity } from "../identity/api";
import { type Account } from "../accounts/api";
import { readEvents, type Run } from "../tasks/api";
import { type Agent } from "../agents/api";
import { type Message } from "./api";
import { ProcessSummary, processStep } from "./ProcessSummary";
import type { RunEvent } from "../tasks/api";
import { runError } from "../tasks/errors";
import type { Skill } from "../skills/api";

/** 管理对话历史、任务流式回复、停止生成及知识库来源。 */
export function ChatView({
  value,
  onChange,
  identity,
  conversationId,
  onConversation,
  onRun,
}: {
  value: string;
  onChange: (value: string) => void;
  identity: Identity;
  account: Account;
  conversationId: string | null;
  onConversation: (id: string | null) => void;
  onRun: (run: Run | null) => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const controller = useRef<AbortController | null>(null);
  const alive = useRef(true);
  // 保留尚未确认成功的提交内容和幂等键；网络失败后重发时，服务端可返回同一个任务。
  const pending = useRef<{
    key: string;
    conversation: string;
    version: string;
    message: string;
    skill: string;
  } | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skill, setSkill] = useState("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [version, setVersion] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [answer, setAnswer] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [steps, setSteps] = useState<string[]>([]);
  const clarifyKey = useRef<{
    answer: string;
    key: string;
    version: number;
  } | null>(null);
  /** 同一任务的每个公开阶段只展示一次，完整多轮记录保留在服务端轨迹中。 */
  function recordStep(event: RunEvent) {
    const step = processStep(event);
    if (step)
      setSteps((previous) =>
        previous.includes(step) ? previous : [...previous, step],
      );
  }
  const scroll = useRef<HTMLDivElement>(null);
  useEffect(() => {
    onRun(run);
  }, [run, onRun]);
  /** 读取已保存的消息和任务，恢复当前对话的可见状态。 */
  async function history(id: string) {
    const rows = await allItems<Message>(`/conversations/${id}/messages`);
    if (alive.current) setMessages(rows);
  }
  /** 监听任务公开事件并更新文本、流程及完成状态。 */
  async function watch(task: Run, id: string) {
    // 一个页面只读取一个任务的事件。停止旧读取不会取消服务端的后台执行。
    controller.current?.abort();
    const reader = new AbortController();
    controller.current = reader;
    setRun(task);
    setBusy(true);
    // 历史回复以任务 ID 作为消息 ID；重放事件前移除它，避免与临时 answer 显示两份。
    setMessages((previous) =>
      previous.filter((message) => message.id !== task.id),
    );
    setAnswer("");
    setError("");
    setSteps([]);
    try {
      await readEvents(task, reader.signal, (event) => {
        if (!alive.current) return;
        recordStep(event);
        // 恢复检查点时覆盖临时文本，平常收到 text_delta 时才追加。
        // 中断前显示过的片段未必已进入检查点，直接追加会造成重复回复。
        if (event.type === "checkpoint_restored") {
          setAnswer(event.payload.text || "");
          setError("");
          setRun((previous) =>
            previous ? { ...previous, status: "running" } : previous,
          );
        }
        if (event.type === "completed") setError("");
        if (event.type === "text_delta")
          setAnswer((previous) => previous + (event.payload.text || ""));
        if (event.type === "tool_status") {
          if (event.payload.stage === "running")
            setRun((previous) =>
              previous ? { ...previous, status: "running" } : previous,
            );
          setStatus(
            event.payload.name
              ? `工具：${event.payload.name} · ${event.payload.status}`
              : "正在处理任务…",
          );
        }
        if (event.type === "error")
          setError(runError(event.payload.error_code || "执行失败"));
      });
      if (reader.signal.aborted || !alive.current) return;
      // 事件读取结束后，以服务端任务状态和持久化消息为准，再清空临时流式文本。
      // 结束也可能代表失败、取消或等待补充信息，不一定是成功。
      const result = await identityRequest<Run>(`/runs/${task.id}`);
      await history(id);
      setRun(result);
      setAnswer("");
      setStatus(
        result.status === "waiting_input"
          ? "请补充信息后继续"
          : result.status === "succeeded"
            ? "任务完成"
            : "任务已结束",
      );
    } catch (failure) {
      if (!reader.signal.aborted && alive.current)
        setError(
          failure instanceof Error
            ? failure.message
            : "连接中断，请重新打开会话。",
        );
    } finally {
      if (!reader.signal.aborted && alive.current) setBusy(false);
    }
  }
  useEffect(() => {
    alive.current = true;
    void allItems<Agent>(`/me/agents`)
      .then((rows) => {
        if (alive.current) {
          setAgents(rows);
          setVersion(rows[0]?.version_id || "");
        }
      })
      .catch((failure) => {
        if (alive.current) setError(failure.message);
      });
    void allItems<Skill>(`/me/skills`)
      .then((rows) => {
        if (alive.current) setSkills(rows);
      })
      .catch(() => {
        /* 技能目录加载失败时保留不使用 Skill 的普通对话。 */
      });
    /** 加载当前页面所需的授权资源，丢弃过期请求结果。 */
    const load = async () => {
      try {
        if (conversationId) {
          await history(conversationId);
          const page = await identityRequest<{ items: Run[] }>(
            `/conversations/${conversationId}/runs`,
          );
          if (!alive.current) return;
          const latest = page.items[0];
          if (latest) {
            setRun(latest);
            setSkill(latest.skill?.version_id || "");
            if (["queued", "running"].includes(latest.status))
              void watch(latest, conversationId);
            else {
              setStatus(
                latest.status === "waiting_input"
                  ? "请补充信息后继续"
                  : latest.status === "succeeded"
                    ? "任务完成"
                    : "任务已结束",
              );
              if (latest.error_code) setError(runError(latest.error_code));
              // 历史会话仅重读流程事件；已经保存的答复不重复拼接。
              const replay = new AbortController();
              controller.current = replay;
              void readEvents(latest, replay.signal, (event) => {
                if (alive.current) recordStep(event);
              }).catch(() => {
                /* 过期的流程日志不影响已保存消息的阅读。 */
              });
            }
          }
        }
      } catch (failure) {
        if (alive.current)
          setError(failure instanceof Error ? failure.message : "加载失败");
      } finally {
        if (alive.current) setLoading(false);
      }
    };
    void load();
    return () => {
      alive.current = false;
      controller.current?.abort();
    };
    // 按空间与会话重新挂载；离开页面只断开读取，服务端任务仍可从历史恢复查看。
  }, []);
  useEffect(() => {
    scroll.current?.scrollTo({
      top: scroll.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, answer]);
  /** 校验输入后提交幂等任务，并开始消费流式回复。 */
  async function send() {
    if (busy || !value.trim() || (!version && run?.status !== "waiting_input"))
      return;
    if (run?.status === "waiting_input") {
      // 知识检索需要澄清时，输入用于恢复原任务；state_version 防止回答过期的问题。
      setBusy(true);
      setError("");
      try {
        if (
          !clarifyKey.current ||
          clarifyKey.current.answer !== value ||
          clarifyKey.current.version !== run.state_version
        )
          clarifyKey.current = {
            answer: value,
            key: crypto.randomUUID(),
            version: run.state_version || 0,
          };
        const task = await identityRequest<Run>(
          `/runs/${run.id}/clarify`,
          "POST",
          { answer: value, state_version: clarifyKey.current.version },
          identity.csrf_token,
          { "Idempotency-Key": clarifyKey.current.key },
        );
        clarifyKey.current = null;
        onChange("");
        await history(conversationId!);
        void watch(task, conversationId!);
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : "补充信息失败");
        setBusy(false);
      }
      return;
    }
    setBusy(true);
    // 新任务确认前清除旧运行，停止按钮不能误取消上一轮任务。
    setRun(null);
    setStatus("正在提交任务…");
    setSteps([]);
    setError("");
    try {
      // 首次发送先创建会话，再把消息提交为后台任务；重发优先使用上次创建的会话。
      let id = pending.current?.conversation || conversationId;
      if (!id)
        id = (
          await identityRequest<{ id: string }>(
            `/me/conversations`,
            "POST",
            { title: value.trim().slice(0, 60) },
            identity.csrf_token,
          )
        ).id;
      // 不确定网络结果复用幂等键，防止重复创建任务与扣额。
      if (
        !pending.current ||
        pending.current.message !== value ||
        pending.current.version !== version ||
        pending.current.skill !== skill
      )
        pending.current = {
          key: crypto.randomUUID(),
          conversation: id,
          version,
          message: value,
          skill,
        };
      const request = pending.current;
      const task = await identityRequest<Run>(
        "/runs",
        "POST",
        {
          conversation_id: request.conversation,
          agent_version_id: request.version,
          message: request.message,
          skill_version_id: request.skill || null,
        },
        identity.csrf_token,
        { "Idempotency-Key": request.key },
      );
      pending.current = null;
      onChange("");
      if (conversationId !== request.conversation) {
        // 切换到新会话后由组件加载逻辑接管 watch，当前实例不再另开一次读取。
        onConversation(request.conversation);
        return;
      }
      await history(request.conversation);
      void watch(task, request.conversation);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "发送失败");
      setStatus("");
      setBusy(false);
    }
  }
  return (
    <section
      className={`chat-view ${messages.length ? "has-messages" : ""}`}
      id="chat-view"
      aria-labelledby="heading"
    >
      <div className="chat-content">
        {!messages.length && (
          <div className="welcome">
            <span className="assistant-symbol" aria-hidden="true">
              <svg viewBox="0 0 100 100">
                <path d="M23 82V43a27 27 0 0 1 54 0v39q0 7-6 2l-7-6-9 9q-5 5-10 0l-9-9-7 6q-6 5-6-2Z" />
                <circle cx="41" cy="48" r="5" />
                <circle cx="61" cy="48" r="5" />
              </svg>
            </span>
            <h1 id="heading">今天，一起完成什么？</h1>
            <p>一个问题，一个想法，或一项复杂任务。</p>
          </div>
        )}
        {messages.length > 0 && (
          <>
            <h1 id="heading" className="sr-only">
              当前对话
            </h1>
            <div className="message-list" ref={scroll} aria-label="对话消息">
              {messages.map((message, index) => (
                <Fragment key={message.id}>
                  {!busy &&
                    index === messages.length - 1 &&
                    message.role === "assistant" && (
                      <ProcessSummary steps={steps} busy={false} />
                    )}
                  <article className={`message ${message.role}`}>
                    <strong>
                      {message.role === "user" ? "你" : "YugiTalk"}
                    </strong>
                    <SafeMarkdown>
                      {message.role === "assistant" && message.sources?.length
                        ? withoutSourceLabels(message.content)
                        : message.content}
                    </SafeMarkdown>
                    <Sources sources={message.sources || []} />
                  </article>
                </Fragment>
              ))}
              {(busy || messages.at(-1)?.role !== "assistant") && (
                <ProcessSummary steps={steps} busy={busy} />
              )}
              {answer && (
                <article className="message assistant">
                  <strong>YugiTalk · 生成中</strong>
                  <SafeMarkdown>
                    {run?.knowledge_base_id
                      ? withoutSourceLabels(answer, busy)
                      : answer}
                  </SafeMarkdown>
                </article>
              )}
            </div>
          </>
        )}
        {!messages.length && busy && (
          <ProcessSummary steps={steps} busy={busy} />
        )}
        {!messages.length && (
          <div className="quick-actions" aria-label="任务灵感">
            {prompts.map((prompt) => (
              <button
                key={prompt.title}
                onClick={() => {
                  onChange(prompt.text);
                  input.current?.focus();
                }}
              >
                <span
                  className={`quick-icon ${prompt.color}`}
                  aria-hidden="true"
                >
                  {prompt.symbol}
                </span>
                {prompt.title}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="chat-bottom">
        {run?.status === "waiting_input" && run.clarification && (
          <aside className="rag-clarification" aria-label="补充信息">
            <p>{run.clarification.prompt}</p>
            {run.clarification.options.map((option) => (
              <button key={option} onClick={() => onChange(option)}>
                {option}
              </button>
            ))}
            <button
              onClick={() => {
                void identityRequest<Run>(
                  `/runs/${run.id}/cancel`,
                  "POST",
                  undefined,
                  identity.csrf_token,
                )
                  .then(setRun)
                  .catch((failure) => setError(failure.message));
              }}
            >
              取消本次问答
            </button>
          </aside>
        )}
        <div className="composer">
          <label className="sr-only" htmlFor="prompt">
            任务输入
          </label>
          <textarea
            ref={input}
            id="prompt"
            maxLength={6000}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder={
              run?.status === "waiting_input"
                ? "补充信息，继续本次问答…"
                : "把你的想法交给 YugiTalk…"
            }
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-bottom">
            <label className="skill-context">
              <span className="sr-only">对话 Skill</span>
              <select
                aria-label="对话 Skill"
                value={skill}
                disabled={busy || run?.status === "waiting_input"}
                onChange={(event) => setSkill(event.target.value)}
              >
                <option value="">不使用 Skill</option>
                {run?.skill &&
                  !skills.some(
                    (row) =>
                      row.enabled && row.version_id === run.skill?.version_id,
                  ) &&
                  skill === run.skill.version_id && (
                    <option value={run.skill.version_id}>
                      {run.skill.name} · v{run.skill.number}（历史版本）
                    </option>
                  )}
                {skills
                  .filter((row) => row.enabled)
                  .map((row) => (
                    <option key={row.id} value={row.version_id}>
                      {row.name} · v{row.number}
                    </option>
                  ))}
              </select>
            </label>
            <label className="input-context">
              <span className="sr-only">选择助手</span>
              <select
                aria-label="选择助手"
                value={version}
                onChange={(event) => setVersion(event.target.value)}
                disabled={busy}
              >
                {!agents.length && (
                  <option value="">请先在 Agent 页创建助手</option>
                )}
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.version_id}>
                    {agent.name} · v{agent.number} · {agent.config.provider}
                  </option>
                ))}
              </select>
            </label>
            {busy ? (
              <button
                className="stop-task"
                aria-label="停止生成"
                title={run ? "停止生成" : "正在提交任务…"}
                disabled={!run}
                onClick={() => {
                  // 任务确认前只显示处理图标，避免取消上一轮或不存在的运行。
                  if (!run) return;
                  void identityRequest<Run>(
                    `/runs/${run.id}/cancel`,
                    "POST",
                    undefined,
                    identity.csrf_token,
                  )
                    .then((result) => {
                      setRun(result);
                      setStatus("任务已停止");
                    })
                    .catch((failure) => setError(failure.message));
                }}
              >
                <span className="stop-square" aria-hidden="true" />
              </button>
            ) : (
              <button
                className="send"
                disabled={
                  loading ||
                  busy ||
                  (!version && run?.status !== "waiting_input") ||
                  !value.trim()
                }
                aria-label={version ? "发送任务" : "发送任务，请先创建助手"}
                onClick={() => void send()}
              >
                <span aria-hidden="true">↑</span>
              </button>
            )}
          </div>
        </div>
        {error && (
          <p role="alert">
            {error}
            {run && ["queued", "running"].includes(run.status) && !busy && (
              <button onClick={() => void watch(run, conversationId!)}>
                重新连接
              </button>
            )}
          </p>
        )}
        <p className="run-status" role="status">
          {status}
          {run &&
            ` · ${run.provider}/${run.model} · 估算用量 ${run.charged_tokens} · 输入/输出 Token ${run.input_tokens ?? "未知"}/${run.output_tokens ?? "未知"}`}
        </p>
        <p className="input-hint">Enter 发送 · Shift + Enter 换行</p>
      </div>
    </section>
  );
}
