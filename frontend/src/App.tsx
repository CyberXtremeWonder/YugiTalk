import { useEffect, useMemo, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { KnowledgeView } from "./rag/KnowledgeView";
import { MemorySettings } from "./memory/MemorySettings";
import { ChatView } from "./chat/ChatView";
import { AgentsView } from "./agents/AgentsView";
import { SkillsView } from "./skills/SkillsView";
import { ToolsView } from "./tools/ToolsView";
import { TasksView } from "./tasks/TasksView";
import { Dashboard } from "./monitoring/Dashboard";
import { TraceView } from "./trace/TraceView";
import type { Conversation } from "./chat/api";
import type { Run } from "./tasks/api";
import { fetchHealth, type Health } from "./health/api";
import { readView, views, type ViewId } from "./shared/views";
import { Login } from "./identity/Login";
import { DevelopmentView } from "./identity/DevelopmentView";
import {
  ApiError,
  allItems,
  fetchIdentity,
  identityRequest,
  type Identity,
} from "./identity/api";

/** 初始化登录身份，并在会话失效时返回登录界面。 */
export function App() {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    /** 记录鼠标操作，输入框点击时不增加额外轮廓。 */
    const pointer = () => {
      document.documentElement.dataset.focusInput = "pointer";
    };
    /** 登录页和工作台统一识别 Tab 导航，保留键盘焦点提示。 */
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Tab")
        document.documentElement.dataset.focusInput = "keyboard";
    };
    document.addEventListener("pointerdown", pointer);
    document.addEventListener("keydown", keyboard);
    void fetchIdentity()
      .then((data) => {
        if (active) setIdentity(data);
      })
      .catch((failure) => {
        if (
          active &&
          (!(failure instanceof ApiError) || failure.status !== 401)
        )
          setError("无法连接服务，请检查后端后刷新页面。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    /** 清除已失效的用户身份，并提示重新登录。 */
    const expired = () => {
      setIdentity(null);
      setError("会话已过期，请重新登录。");
    };
    window.addEventListener("session-expired", expired);
    return () => {
      active = false;
      window.removeEventListener("session-expired", expired);
      document.removeEventListener("pointerdown", pointer);
      document.removeEventListener("keydown", keyboard);
    };
  }, []);
  if (loading)
    return (
      <main className="login-shell">
        <p role="status">正在确认登录状态…</p>
      </main>
    );
  if (!identity)
    return (
      <Login
        initialError={error}
        onLogin={(data) => {
          setIdentity(data);
          setError("");
        }}
      />
    );
  return (
    <Workbench
      identity={identity}
      onRefresh={async () => {
        try {
          setIdentity(await fetchIdentity());
        } catch (failure) {
          if (failure instanceof ApiError && failure.status === 401) {
            setIdentity(null);
            setError("会话已过期，请重新登录。");
          } else throw failure;
        }
      }}
      onLogout={() => {
        setIdentity(null);
        setError("");
      }}
    />
  );
}

/** 组装侧栏导航、空间切换与各功能页面，维护工作台交互状态。 */
function Workbench({
  identity,
  onRefresh,
  onLogout,
}: {
  identity: Identity;
  onRefresh: () => Promise<void>;
  onLogout: () => void;
}) {
  const currentAccount = useMemo(
    () => ({
      id: identity.user.id,
      name: identity.user.name,
    }),
    [identity.user.id, identity.user.name],
  );
  const [logoutError, setLogoutError] = useState("");
  const [view, setView] = useState<ViewId>(readView);
  const [draft, setDraft] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [currentRun, setCurrentRun] = useState<Run | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [historyError, setHistoryError] = useState("");
  useEffect(() => {
    // 空间删除或切换后清空旧会话引用，避免访问上一空间的数据。
    setConversationId(null);
    setCurrentRun(null);
    setConversations([]);
  }, [currentAccount?.id]);
  useEffect(() => {
    let active = true;
    setHistoryError("");
    if (currentAccount)
      void allItems<Conversation>(`/me/conversations`)
        .then((rows) => {
          if (active) setConversations(rows);
        })
        .catch((failure) => {
          if (active) setHistoryError(failure.message);
        });
    return () => {
      active = false;
    };
  }, [currentAccount, conversationId, view]);
  const [dark, setDark] = useState(false);
  const [menu, setMenu] = useState(false);
  const [panel, setPanel] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState("");
  const current = views[view];
  /** 切换当前功能页面并同步地址栏位置。 */
  function navigate(next: ViewId) {
    setView(next);
    setMenu(false);
    window.history.pushState(null, "", `#${next}`);
  }
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }, [dark]);
  useEffect(() => {
    /** 使用 Escape 收起移动端菜单，键盘焦点由应用入口统一管理。 */
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenu(false);
    };
    /** 将地址栏位置同步到工作台当前页面。 */
    const route = () => {
      setView(readView());
      setMenu(false);
    };
    /** 根据窗口尺寸更新界面布局状态。 */
    const resize = () => {
      setPanel(false);
      setMenu(false);
    };
    const viewport = window.matchMedia("(max-width: 640px)");
    document.addEventListener("keydown", keyboard);
    window.addEventListener("hashchange", route);
    window.addEventListener("popstate", route);
    viewport.addEventListener("change", resize);
    return () => {
      document.removeEventListener("keydown", keyboard);
      window.removeEventListener("hashchange", route);
      window.removeEventListener("popstate", route);
      viewport.removeEventListener("change", resize);
    };
  }, []);
  useEffect(() => {
    if (view !== "settings") return;
    // 页面切换时取消请求，避免卸载后更新；超时也要向用户反馈。
    const controller = new AbortController();
    let active = true;
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    setHealth(null);
    setHealthError("");
    void fetchHealth(controller.signal)
      .then((result) => {
        if (active) setHealth(result);
      })
      .catch(() => {
        if (active) setHealthError("暂时无法连接服务，请检查后端与部署状态。");
      });
    return () => {
      active = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [view]);
  return (
    <Dialog.Root open={panel} onOpenChange={setPanel}>
      <a
        className="skip-link"
        href={view === "chat" ? "#prompt" : "#main-panel"}
      >
        {view === "chat" ? "跳到任务输入" : "跳到主要内容"}
      </a>
      <div className="app-shell">
        <aside className={`sidebar ${menu ? "open" : ""}`} id="sidebar">
          <a
            className="brand"
            href="#chat"
            aria-label="YugiTalk 首页"
            onClick={(event) => {
              event.preventDefault();
              navigate("chat");
            }}
          >
            <span className="brand-mark" aria-hidden="true">
              Y↗
            </span>
            yugitalk<span className="brand-dot">.</span>
          </a>

          <button
            className="new-chat"
            id="new-chat"
            onClick={() => {
              navigate("chat");
              setDraft("");
              setConversationId(null);
              requestAnimationFrame(() =>
                document.getElementById("prompt")?.focus(),
              );
            }}
          >
            <span aria-hidden="true">＋</span> 新建对话
          </button>
          <nav id="navigation" aria-label="主要导航">
            {(
              [
                "chat",
                "agents",
                "skills",
                "tools",
                "knowledge",
                "tasks",
              ] as const
            ).map((key) => (
              <button
                key={key}
                aria-current={view === key ? "page" : undefined}
                onClick={() => navigate(key)}
              >
                <span
                  className={`nav-icon ${views[key].color}`}
                  aria-hidden="true"
                >
                  {views[key].symbol}
                </span>
                {views[key].title}
              </button>
            ))}
          </nav>
          {identity.user.platform_admin && (
            <button
              className="sidebar-option"
              onClick={() => navigate("monitoring")}
              aria-current={view === "monitoring" ? "page" : undefined}
            >
              ▥ 实时监控
            </button>
          )}
          {identity.user.platform_admin && (
            <button
              className="sidebar-option"
              onClick={() => navigate("trace")}
              aria-current={view === "trace" ? "page" : undefined}
            >
              ≋ Trace
            </button>
          )}
          {identity.user.platform_admin && (
            <button
              className="sidebar-option"
              onClick={() => {
                if (identity.development_mode) {
                  navigate("development");
                  return;
                }
                // 管理员未在登录页勾选开发模式时，也可从入口显式开启。
                void identityRequest(
                  "/auth/development-mode",
                  "POST",
                  undefined,
                  identity.csrf_token,
                )
                  .then(onRefresh)
                  .then(() => navigate("development"))
                  .catch((failure) => setLogoutError(failure.message));
              }}
              aria-current={view === "development" ? "page" : undefined}
            >
              ⌘ 管理员面板
            </button>
          )}
          <section className="history" aria-labelledby="history-title">
            <h2 id="history-title">最近对话</h2>
            {historyError && <p role="alert">{historyError}</p>}
            {!conversations.length && <p>从一个新的想法开始。</p>}
            {conversations.map((conversation) => (
              <div key={conversation.id} className="history-item">
                <button
                  aria-current={
                    conversationId === conversation.id ? "page" : undefined
                  }
                  onClick={() => {
                    setConversationId(conversation.id);
                    setDraft("");
                    navigate("chat");
                  }}
                >
                  {conversation.title}
                </button>
                <details>
                  <summary>管理对话</summary>
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      const form = new FormData(event.currentTarget);
                      const title = String(form.get("title") || "").trim();
                      void identityRequest(
                        `/conversations/${conversation.id}`,
                        "PATCH",
                        { title },
                        identity.csrf_token,
                      )
                        .then(() =>
                          setConversations((rows) =>
                            rows.map((row) =>
                              row.id === conversation.id
                                ? { ...row, title }
                                : row,
                            ),
                          ),
                        )
                        .catch((failure) => setHistoryError(failure.message));
                    }}
                  >
                    <input
                      name="title"
                      aria-label={`重命名 ${conversation.title}`}
                      required
                      maxLength={80}
                      defaultValue={conversation.title}
                    />
                    <button>保存名称</button>
                  </form>
                  <button
                    onClick={() => {
                      void identityRequest(
                        `/conversations/${conversation.id}`,
                        "DELETE",
                        undefined,
                        identity.csrf_token,
                      )
                        .then(() => {
                          setConversations((rows) =>
                            rows.filter((row) => row.id !== conversation.id),
                          );
                          if (conversationId === conversation.id)
                            setConversationId(null);
                        })
                        .catch((failure) => setHistoryError(failure.message));
                    }}
                  >
                    删除对话及消息
                  </button>
                </details>
              </div>
            ))}
          </section>
          <div className="sidebar-bottom">
            <Dialog.Trigger asChild>
              <button className="sidebar-option" id="panel">
                <span aria-hidden="true">☷</span>
                <span>{panel ? "收起详情" : "展开详情"}</span>
              </button>
            </Dialog.Trigger>
            <button
              className="sidebar-option"
              id="theme"
              aria-label={`切换到${dark ? "浅" : "深"}色主题`}
              onClick={() => setDark(!dark)}
            >
              <span aria-hidden="true">◐</span>
              <span>{dark ? "浅" : "深"}色主题</span>
            </button>
            <button
              className="sidebar-option"
              aria-current={view === "settings" ? "page" : undefined}
              onClick={() => navigate("settings")}
            >
              <span aria-hidden="true">⚙</span>设置
            </button>
            <div className="profile">
              <span className="avatar">Y</span>
              <div>
                <strong>{identity.user.name}</strong>
                <span>你的知识与行动，在这里连接</span>
              </div>
            </div>
          </div>
          <button
            className="sidebar-option"
            onClick={() => {
              void identityRequest(
                "/auth/logout",
                "POST",
                undefined,
                identity.csrf_token,
              )
                .then(onLogout)
                .catch((failure) => setLogoutError(failure.message));
            }}
          >
            退出登录
          </button>
          {logoutError && <p role="alert">{logoutError}</p>}
        </aside>
        <main className="main-panel" id="main-panel" tabIndex={-1}>
          <header className="topbar">
            <div className="header-title">
              <button
                id="menu"
                className="menu-button"
                aria-expanded={menu}
                aria-controls="navigation"
                onClick={() => setMenu(!menu)}
              >
                导航 <span aria-hidden="true">☰</span>
              </button>
              <a
                className="mobile-brand"
                href="#chat"
                aria-label="YugiTalk 手机首页"
                onClick={(event) => {
                  event.preventDefault();
                  navigate("chat");
                }}
              >
                yugitalk<span className="brand-dot">.</span>
              </a>
              <span id="page-title">{current.title}</span>
              <span className="model-label">通用助手</span>
            </div>
            <span className="preview-badge">
              {identity.development_mode ? "管理员 · 开发模式" : "工作台"}
            </span>
          </header>
          {view === "chat" ? (
            currentAccount && (
              <ChatView
                key={`${currentAccount.id}:${conversationId}`}
                value={draft}
                onChange={setDraft}
                identity={identity}
                account={currentAccount}
                conversationId={conversationId}
                onConversation={setConversationId}
                onRun={setCurrentRun}
              />
            )
          ) : view === "monitoring" ? (
            identity.user.platform_admin ? (
              <Dashboard />
            ) : (
              <section className="secondary-view">
                <p role="alert">实时监控仅限管理员访问。</p>
              </section>
            )
          ) : view === "trace" ? (
            identity.user.platform_admin ? (
              <TraceView />
            ) : (
              <section className="secondary-view">
                <p role="alert">Trace 仅限管理员访问。</p>
              </section>
            )
          ) : view === "development" ? (
            identity.user.platform_admin && identity.development_mode ? (
              <DevelopmentView identity={identity} />
            ) : (
              <section className="secondary-view">
                <p role="alert">管理员面板仅限开发模式下的管理员访问。</p>
              </section>
            )
          ) : view === "agents" && currentAccount ? (
            <AgentsView
              key={currentAccount.id}
              identity={identity}
              account={currentAccount}
            />
          ) : view === "skills" && currentAccount ? (
            <SkillsView
              key={currentAccount.id}
              identity={identity}
              account={currentAccount}
            />
          ) : view === "tools" && currentAccount ? (
            <ToolsView
              key={currentAccount.id}
              identity={identity}
              account={currentAccount}
            />
          ) : view === "knowledge" && currentAccount ? (
            <KnowledgeView
              key={currentAccount.id}
              identity={identity}
              account={currentAccount}
            />
          ) : view === "tasks" && currentAccount ? (
            <TasksView
              csrf={identity.csrf_token}
              key={currentAccount.id}
              accountId={currentAccount.id}
              onOpen={(id) => {
                setConversationId(id);
                navigate("chat");
              }}
            />
          ) : (
            <section
              className="secondary-view"
              id="secondary-view"
              aria-labelledby="view-title"
            >
              <span className="view-symbol" aria-hidden="true">
                {current.symbol}
              </span>
              <h1 id="view-title">{current.title}</h1>
              <p>{current.description}</p>
              <div className="empty-state">
                <span>{current.empty}</span>
                <p>{current.hint}</p>
                {view === "settings" && (
                  <div role="status" aria-label="服务连接状态">
                    {healthError ||
                      (health
                        ? `API：${health.status}；${Object.entries(
                            health.checks,
                          )
                            .map(
                              ([name, ok]) =>
                                `${name} ${ok ? "正常" : "不可用"}`,
                            )
                            .join("；")}`
                        : "正在检查服务…")}
                  </div>
                )}
              </div>
              {view === "settings" && (
                <>
                  {currentAccount && (
                    <MemorySettings
                      key={currentAccount?.id}
                      identity={identity}
                      accountId={currentAccount?.id || ""}
                    />
                  )}
                </>
              )}
            </section>
          )}
          <footer className="status-footer">
            <span>YUGITALK / YOUR IDEAS, IN ACTION.</span>
          </footer>
        </main>
      </div>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-backdrop" />
        <Dialog.Content
          className="inspector"
          id="inspector"
          aria-label="任务详情"
        >
          <div className="inspector-header">
            <span>任务详情</span>
            <Dialog.Close asChild>
              <button className="close-button" aria-label="关闭任务详情">
                ×
              </button>
            </Dialog.Close>
          </div>
          <Dialog.Title>每一步，都清晰。</Dialog.Title>
          <Dialog.Description className="inspector-intro">
            当前会话任务的公开状态与用量；不展示模型内部推理。
          </Dialog.Description>
          <p>
            {currentRun
              ? `状态：${currentRun.status}；预算额度：${currentRun.charged_tokens}；输入/输出 Token：${currentRun.input_tokens ?? "未知"}/${currentRun.output_tokens ?? "未知"}`
              : "尚未执行任务"}
          </p>
          <div className="run-info">
            <span>模型与费用</span>
            <span>
              {currentRun
                ? `${currentRun.provider}/${currentRun.model}；未配置价格，费用未知`
                : "尚未连接"}
            </span>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
