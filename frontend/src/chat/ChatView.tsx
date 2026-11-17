import { Fragment, useEffect, useRef, useState } from "react";
import { prompts } from "../shared/views";
import { allItems, identityRequest, type Identity } from "../identity/api";
import type { Account } from "../accounts/api";
import type { Run } from "../tasks/api";
import type { Agent } from "../agents/api";
import type { Skill } from "../skills/api";
import type { Message } from "./api";
import { ChatTask, activeTask } from "./ChatTask";
import { SafeMarkdown } from "./SafeMarkdown";
import { Attachments } from "./Attachments";
import { Sources } from "../rag/Sources";
import { uploadChatFile, type ChatFile } from "./uploads";
import "./uploads.css";

/** 对话允许多个后台任务；输入提交状态与各任务的执行状态分别维护。 */
export function ChatView({
  value,
  onChange,
  agentVersion = "",
  onAgentVersion,
  identity,
  conversationId,
  onConversation,
  onRun,
}: {
  value: string;
  onChange: (value: string) => void;
  agentVersion?: string;
  onAgentVersion?: (version: string) => void;
  identity: Identity;
  account: Account;
  conversationId: string | null;
  onConversation: (id: string | null) => void;
  onRun: (run: Run | null) => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<ChatFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const alive = useRef(true);
  const sending = useRef(false);
  const draft = useRef(value);
  draft.current = value;
  const pending = useRef<{
    key: string;
    conversation: string;
    version: string;
    message: string;
    skill: string;
    files: string;
  } | null>(null);
  const clarifyKey = useRef<{
    answer: string;
    key: string;
    version: number;
  } | null>(null);
  const steerKey = useRef<{ run: string; message: string; key: string } | null>(
    null,
  );
  const [steeringTarget, setSteeringTarget] = useState("auto");
  const historyRequest = useRef(0);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skill, setSkill] = useState("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [version, setVersion] = useState(agentVersion);
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [clarification, setClarification] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [controlling, setControlling] = useState(false);
  const controlPending = useRef(false);
  const [loading, setLoading] = useState(true);
  const scroll = useRef<HTMLDivElement>(null);
  const steerable = runs.filter(
    (task) => activeTask(task) && task.steering_supported !== false,
  );
  const steeringTask =
    clarification || steeringTarget === "new"
      ? null
      : steeringTarget === "auto"
        ? steerable.at(-1)
        : steerable.find((task) => task.id === steeringTarget);

  async function history(id: string) {
    const serial = ++historyRequest.current;
    const rows = await allItems<Message>(`/conversations/${id}/messages`);
    if (alive.current && serial === historyRequest.current) setMessages(rows);
  }

  function updateTask(task: Run) {
    if (!alive.current) return;
    setRuns((previous) =>
      previous.some((r) => r.id === task.id)
        ? previous.map((r) =>
            r.id === task.id &&
            (task.event_sequence || 0) >= (r.event_sequence || 0)
              ? task
              : r,
          )
        : [...previous, task],
    );
    setClarification((selected) =>
      selected?.id === task.id ? task : selected,
    );
  }

  useEffect(() => {
    onRun(runs.at(-1) || null);
  }, [runs, onRun]);

  useEffect(() => {
    alive.current = true;
    void allItems<Agent>("/me/agents")
      .then((rows) => {
        if (alive.current) {
          setAgents(rows);
          // 功能切换会重新挂载；优先使用工作台保留的选择，只在已不可用时回退。
          const selected = rows.some((row) => row.version_id === agentVersion)
            ? agentVersion
            : rows[0]?.version_id || "";
          setVersion(selected);
          onAgentVersion?.(selected);
        }
      })
      .catch((failure) => {
        if (alive.current) setError(failure.message);
      });
    void allItems<Skill>("/me/skills")
      .then((rows) => {
        if (alive.current) setSkills(rows);
      })
      .catch(() => {
        /* 技能列表失败不阻断普通对话。 */
      });
    void (async () => {
      try {
        if (conversationId) {
          await history(conversationId);
          const page = await identityRequest<{ items: Run[] }>(
            `/conversations/${conversationId}/runs`,
          );
          if (!alive.current) return;
          setRuns([...page.items].reverse());
          setSkill(page.items[0]?.skill?.version_id || "");
        }
      } catch (failure) {
        if (alive.current)
          setError(failure instanceof Error ? failure.message : "加载失败");
      } finally {
        if (alive.current) setLoading(false);
      }
    })();
    return () => {
      alive.current = false;
    };
    // App 按会话重新挂载，离开只断开监听，后台任务继续执行。
  }, []);

  useEffect(() => {
    scroll.current?.scrollTo({
      top: scroll.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  function selectClarification(task: Run, answer?: string) {
    steerKey.current = null;
    setClarification(task);
    if (answer !== undefined) onChange(answer);
    setError("");
    input.current?.focus();
  }

  async function send() {
    if (
      !clarification &&
      !["auto", "new"].includes(steeringTarget) &&
      !steeringTask &&
      steerKey.current?.message !== value
    ) {
      setError("所选任务已结束，请选择其他插话目标或新任务。");
      return;
    }
    if (steeringTask && files.length) {
      setError("插话仅支持文字；发送附件请先选择新任务。");
      return;
    }
    if (clarification && files.length) {
      setError("附件请作为新消息发送，请先取消补充目标。");
      return;
    }
    if (
      sending.current ||
      uploading ||
      (!value.trim() && !files.length) ||
      (!version && !clarification && !steeringTask && steerKey.current?.message !== value)
    )
      return;
    sending.current = true;
    setSubmitting(true);
    setError("");
    const text = value.trim() ? value : "请阅读并分析附件中的内容。";
    try {
      let id = pending.current?.conversation || conversationId;
      if (!id)
        id = (
          await identityRequest<{ id: string }>(
            "/me/conversations",
            "POST",
            { title: text.trim().slice(0, 60) },
            identity.csrf_token,
          )
        ).id;
      let task: Run;
      if (
        steeringTask ||
        (!clarification &&
          steerKey.current?.message === text &&
          steeringTarget !== "new")
      ) {
        const target = steerKey.current?.message === text
          ? steerKey.current.run
          : steeringTask!.id;
        if (
          steerKey.current?.run !== target ||
          steerKey.current.message !== text
        )
          steerKey.current = {
            run: target,
            message: text,
            key: crypto.randomUUID(),
          };
        task = await identityRequest<Run>(
          `/runs/${target}/steer`,
          "POST",
          { message: text },
          identity.csrf_token,
          { "Idempotency-Key": steerKey.current!.key },
        );
        steerKey.current = null;
      } else if (clarification?.status === "waiting_input") {
        if (
          !clarifyKey.current ||
          clarifyKey.current.answer !== text ||
          clarifyKey.current.version !== clarification.state_version
        )
          clarifyKey.current = {
            answer: text,
            key: crypto.randomUUID(),
            version: clarification.state_version || 0,
          };
        task = await identityRequest<Run>(
          `/runs/${clarification.id}/clarify`,
          "POST",
          { answer: text, state_version: clarifyKey.current.version },
          identity.csrf_token,
          { "Idempotency-Key": clarifyKey.current.key },
        );
        clarifyKey.current = null;
      } else {
        if (
          !pending.current ||
          pending.current.message !== text ||
          pending.current.version !== version ||
          pending.current.skill !== skill ||
          pending.current.files !== JSON.stringify(files)
        )
          pending.current = {
            key: crypto.randomUUID(),
            conversation: id,
            version,
            message: text,
            skill,
            files: JSON.stringify(files),
          };
        const request = pending.current;
        task = await identityRequest<Run>(
          "/runs",
          "POST",
          {
            conversation_id: request.conversation,
            agent_version_id: request.version,
            message: request.message,
            skill_version_id: request.skill || null,
            uploaded_files: JSON.parse(request.files),
          },
          identity.csrf_token,
          { "Idempotency-Key": request.key },
        );
        pending.current = null;
      }
      if (!alive.current) return;
      setFiles([]);
      if (draft.current === text || !draft.current.trim()) onChange("");
      setClarification(null);
      if (conversationId !== id) {
        onConversation(id);
        return;
      }
      await history(id);
      updateTask(task);
    } catch (failure) {
      const reason = failure instanceof Error ? failure.message : "发送失败";
      setError(reason);
    } finally {
      sending.current = false;
      if (alive.current) setSubmitting(false);
    }
  }

  /** 暂停、继续与停止都作用于指定任务，正文输入和附件保持不变。 */
  async function controlTask(task: Run, action: "pause" | "resume" | "cancel") {
    if (controlPending.current) return;
    controlPending.current = true;
    setControlling(true);
    setError("");
    try {
      const result = await identityRequest<Run>(
        `/runs/${task.id}/${action}`,
        "POST",
        undefined,
        identity.csrf_token,
      );
      if (!alive.current) return;
      if (action === "cancel" && conversationId) {
        try {
          await history(conversationId);
        } finally {
          updateTask(result);
        }
      } else updateTask(result);
    } catch (failure) {
      if (alive.current)
        setError(
          failure instanceof Error ? failure.message : "任务控制失败，请重试。",
        );
    } finally {
      controlPending.current = false;
      if (alive.current) setControlling(false);
    }
  }

  const replies = new Map(
    messages.filter((m) => m.role === "assistant").map((m) => [m.id, m]),
  );
  const taskIds = new Set(runs.map((r) => r.id));
  const origins = new Map(runs.map((r) => [r.message_sequence, r]));
  const attached = new Set<string>();
  const current = runs.at(-1);
  const controlledTask = runs.filter(activeTask).at(-1);

  function taskView(task: Run) {
    attached.add(task.id);
    return (
      <ChatTask
        key={task.id}
        task={task}
        message={replies.get(task.id)}
        onControl={controlTask}
        controlling={controlling}
        onTask={updateTask}
        onHistory={() => history(conversationId!)}
        onClarification={selectClarification}
        replay={task.id === runs.at(-1)?.id}
      />
    );
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
              {messages.map((message) => {
                if (message.role === "assistant" && taskIds.has(message.id))
                  return null;
                const task =
                  message.role === "user"
                    ? runs.find((r) => r.id === message.run_id) ||
                      origins.get(message.sequence)
                    : undefined;
                return (
                  <Fragment key={message.id}>
                    <article className={`message ${message.role}`}>
                      <strong>
                        {message.role === "user" ? "你" : "YugiTalk"}
                      </strong>
                      <SafeMarkdown>{message.content}</SafeMarkdown>
                      {!!message.uploaded_files?.length && (
                        <ul
                          className="chat-upload-list"
                          aria-label="已发送附件"
                        >
                          {message.uploaded_files.map((file, index) => (
                            <li key={index}>
                              {file.filename}
                              {file.truncated ? "（文本已截取）" : ""}
                            </li>
                          ))}
                        </ul>
                      )}
                      <Sources sources={message.sources || []} />
                      <Attachments files={message.attachments || []} />
                    </article>
                    {task && taskView(task)}
                  </Fragment>
                );
              })}
              {runs.filter((r) => !attached.has(r.id)).map(taskView)}
            </div>
          </>
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
        {clarification && (
          <aside className="task-clarification" aria-label="补充目标">
            <span>
              补充信息：
              {clarification.prompt_preview || "当前任务"}
            </span>
            <button
              onClick={() => setClarification(null)}
              aria-label="取消补充目标"
            >
              ×
            </button>
          </aside>
        )}
        <div className="composer">
          {files.length > 0 && (
            <ul className="chat-upload-list" aria-label="待发送附件">
              {files.map((file, index) => (
                <li key={index}>
                  <span title={file.filename}>
                    {file.filename}
                    {file.truncated ? "（正文较长，仅发送前 60 KB 文本）" : ""}
                  </span>
                  <button
                    type="button"
                    aria-label={`移除 ${file.filename}`}
                    disabled={submitting}
                    onClick={() =>
                      setFiles((previous) =>
                        previous.filter((_, i) => i !== index),
                      )
                    }
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
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
              clarification
                ? "补充信息，继续本次问答…"
                : steeringTask
                  ? steeringTask.status === "paused"
                    ? "补充指令，继续思考后生效…"
                    : "插话给当前任务，下一次模型调用前生效…"
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
            <input
              ref={fileInput}
              type="file"
              multiple
              className="sr-only"
              aria-label="选择对话附件"
              accept=".docx,.pdf,.txt,.md,.csv,.json,.log,.rst"
              disabled={uploading || submitting || !!clarification}
              onChange={async (event) => {
                const chosen = Array.from(event.target.files || []);
                event.target.value = "";
                if (!chosen.length) return;
                if (files.length + chosen.length > 3) {
                  setError("每条消息最多添加 3 个附件。");
                  return;
                }
                setUploading(true);
                setError("");
                try {
                  const parsed: ChatFile[] = [];
                  for (const file of chosen)
                    parsed.push(
                      await uploadChatFile(file, identity.csrf_token),
                    );
                  if (
                    files.reduce(
                      (sum, f) => sum + new TextEncoder().encode(f.text).length,
                      0,
                    ) +
                      parsed.reduce(
                        (sum, f) =>
                          sum + new TextEncoder().encode(f.text).length,
                        0,
                      ) >
                    90000
                  )
                    throw new Error(
                      "附件文本总计超过 90 KB，请分多条消息发送。",
                    );
                  if (alive.current)
                    setFiles((previous) => [...previous, ...parsed]);
                } catch (failure) {
                  if (alive.current)
                    setError(
                      failure instanceof Error
                        ? failure.message
                        : "上传失败，请重试。",
                    );
                } finally {
                  if (alive.current) setUploading(false);
                }
              }}
            />
            <button
              className="chat-upload-button"
              type="button"
              title="上传 Word、PDF、TXT 等文本文件"
              aria-label="上传文件"
              disabled={
                uploading || submitting || !!clarification || files.length >= 3
              }
              onClick={() => fileInput.current?.click()}
            >
              <span aria-hidden="true">＋</span>
              <span>{uploading ? "解析中…" : "上传文件"}</span>
            </button>
            <label className="skill-context">
              <span className="sr-only">对话 Skill</span>
              <select
                aria-label="对话 Skill"
                value={skill}
                disabled={submitting || !!clarification}
                onChange={(event) => setSkill(event.target.value)}
              >
                <option value="">不使用 Skill</option>
                {current?.skill &&
                  !skills.some(
                    (s) => s.version_id === current.skill?.version_id,
                  ) && (
                    <option value={current.skill.version_id}>
                      {current.skill.name} · v{current.skill.number}（历史版本）
                    </option>
                  )}
                {skills
                  .filter((s) => s.enabled)
                  .map((s) => (
                    <option key={s.id} value={s.version_id}>
                      {s.name} · v{s.number}
                    </option>
                  ))}
              </select>
            </label>
            <label className="input-context">
              <span className="sr-only">选择助手</span>
              <select
                aria-label="选择助手"
                value={version}
                onChange={(event) => {
                  setVersion(event.target.value);
                  onAgentVersion?.(event.target.value);
                }}
                disabled={submitting || !!clarification}
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
            <div className="composer-controls">
              {steerable.length > 0 && !clarification && (
                <select
                  className="steering-target"
                  aria-label="消息目标"
                  value={steeringTarget}
                  disabled={submitting}
                  onChange={(event) => {
                    setSteeringTarget(event.target.value);
                    steerKey.current = null;
                  }}
                >
                  <option value="auto">插话给最近任务</option>
                  {steerable.map((task) => (
                    <option key={task.id} value={task.id}>
                      插话：{task.prompt_preview || task.id.slice(0, 8)}
                    </option>
                  ))}
                  <option value="new">发送新任务</option>
                </select>
              )}
              {controlledTask && (
                <button
                  type="button"
                  className="pause-thinking"
                  disabled={controlling}
                  onClick={() =>
                    void controlTask(
                      controlledTask,
                      controlledTask.status === "paused" ? "resume" : "pause",
                    )
                  }
                >
                  {controlledTask.status === "paused" ? "继续思考" : "暂停思考"}
                </button>
              )}
              {controlledTask && (
                <button
                  type="button"
                  className="pause-thinking"
                  aria-label="停止思考"
                  title="停止思考"
                  disabled={controlling}
                  onClick={() => void controlTask(controlledTask, "cancel")}
                >
                  ■
                </button>
              )}
              <button
                type="button"
                className="send"
                disabled={
                  loading ||
                  submitting ||
                  uploading ||
                  (!version && !clarification && !steeringTask) ||
                  (!value.trim() && !files.length)
                }
                aria-label={
                  steeringTask
                    ? "发送插话"
                    : version
                      ? "发送任务"
                      : "发送任务，请先创建助手"
                }
                title={steeringTask ? "发送插话" : "发送任务"}
                onClick={() => void send()}
              >
                <span aria-hidden="true">↑</span>
              </button>
            </div>
          </div>
        </div>
        {error && <p role="alert">{error}</p>}
        <p className="run-status" role="status">
          {submitting
            ? "正在提交…"
            : controlledTask?.status === "paused"
              ? "已暂停思考"
              : runs.some(activeTask)
                ? `${runs.filter((task) => activeTask(task) && task.status !== "paused").length} 个任务正在处理`
                : current?.status === "succeeded"
                  ? "任务完成"
                  : ""}
          {current &&
            ` · ${current.provider}/${current.model} · 估算用量 ${current.charged_tokens} · 输入/输出 Token ${current.input_tokens ?? "未知"}/${current.output_tokens ?? "未知"}`}
        </p>
        <p className="input-hint">
          Enter 发送 · Shift + Enter 换行 · 附件最多 3 个，单个 20 MiB
        </p>
      </div>
    </section>
  );
}
