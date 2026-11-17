import { useNotice } from "../shared/useNotice";
import type { ToolEntry } from "../tools/api";
import "../shared/configuration.css";
import { ToolChoices } from "../tools/ToolChoices";
import { useEffect, useRef, useState } from "react";
import { allItems, identityRequest, type Identity } from "../identity/api";
import type { Account } from "../accounts/api";
import type { Skill } from "./api";

const template =
  "# 工作流程\n\n1. 明确任务目标与输入。\n2. 使用已授权工具处理资料。\n3. 给出简洁结果，并说明缺失信息。";

/** 管理空间共享技能，通过不可变版本将自定义流程用于后续对话。 */
export function SkillsView({
  identity,
  account,
}: {
  identity: Identity;
  account: Account;
}) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selected, setSelected] = useState<Skill | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState(template);
  const [tools, setTools] = useState<string[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [catalog, setCatalog] = useState<ToolEntry[]>([]);
  const toolSection = useRef<HTMLDetailsElement>(null);
  const { notice, setNotice, noticeIsError, reportError } = useNotice();
  const alive = useRef(true);
  const path = `/me/skills`;
  const canManage = !selected || selected.can_manage;
  /** 刷新技能摘要目录，已删除条目由服务端过滤。 */
  async function refresh() {
    const rows = await allItems<Skill>(path);
    if (alive.current) setSkills(rows);
    return rows;
  }
  useEffect(() => {
    alive.current = true;
    void refresh()
      .then((rows) => {
        if (alive.current && rows[0]) void open(rows[0]);
      })
      .catch((error) => {
        if (alive.current) reportError(error.message);
      });
    return () => {
      alive.current = false;
    };
    // 切换空间由父组件重新挂载，草稿不会带入其他空间。
  }, []);
  /** 清空编辑器以创建新技能，不修改已有条目。 */
  function reset() {
    setSelected(null);
    setName("");
    setDescription("");
    setInstructions(template);
    setTools([]);
    setEnabled(true);
  }
  /** 加载完整正文；目录按钮在请求中禁用以避免慢响应覆盖新的选择。 */
  async function open(row: Skill) {
    setBusy(true);
    setNotice("");
    try {
      const detail = await identityRequest<Skill>(`/skills/${row.id}`);
      // 目录仅含摘要，编辑时才读取完整正文；拿到详情后一次填充各草稿字段。
      if (!alive.current) return;
      setSelected(detail);
      setName(detail.name);
      setDescription(detail.description);
      setInstructions(detail.instructions || "");
      setTools(detail.allowed_tools);
      setEnabled(detail.enabled);
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "读取技能失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  /** 保存一个新版本，不覆盖已创建任务的技能说明。 */
  async function save() {
    setBusy(true);
    setNotice("");
    try {
      const result = await identityRequest<Skill>(
        // 新建与追加版本共享表单；selected 决定目标路径，旧任务仍引用旧版本正文。
        selected ? `/skills/${selected.id}/versions` : path,
        "POST",
        { name, description, instructions, allowed_tools: tools, enabled },
        identity.csrf_token,
      );
      if (!alive.current) return;
      setSelected(result);
      await refresh();
      setNotice(
        `Skill 已发布：${result.name} · v${result.number}。可回到对话页选择。`,
      );
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "保存技能失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  /** 启停或删除技能；删除后清空编辑器，目录不再显示该条目。 */
  async function changeState(remove = false) {
    if (!selected) return;
    setBusy(true);
    setNotice("");
    try {
      await identityRequest(
        `/skills/${selected.id}`,
        remove ? "DELETE" : "PATCH",
        remove ? undefined : { enabled: !selected.enabled },
        identity.csrf_token,
      );
      if (!alive.current) return;
      if (remove) reset();
      // 启停只切换身份状态，不发布新正文；同时同步选中条目和表单开关。
      else {
        setSelected({ ...selected, enabled: !selected.enabled });
        setEnabled(!selected.enabled);
      }
      await refresh();
      setNotice(
        remove
          ? "Skill 已删除。"
          : selected.enabled
            ? "Skill 已停用。"
            : "Skill 已启用。",
      );
    } catch (error) {
      if (alive.current)
        reportError(error instanceof Error ? error.message : "更新技能失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  return (
    <section
      className="secondary-view configuration-workspace"
      aria-labelledby="view-title"
    >
      <header className="cf-heading">
        <div>
          <h1 id="view-title">Skill</h1>
          <p>把常用流程，变成可复用的技能。</p>
        </div>
      </header>
      {notice && (
        <p
          className="cf-notice"
          data-error={noticeIsError}
          role={noticeIsError ? "alert" : "status"}
        >
          {notice}
        </p>
      )}
      <div className="cf-three-columns">
        <aside className="cf-catalog" aria-label="我的技能">
          <h2>
            我的技能 <span className="cf-count">{skills.length}</span>
          </h2>
          <button
            className="cf-primary cf-catalog-create"
            disabled={busy}
            onClick={() => {
              reset();
              setNotice("");
            }}
          >
            ＋ 新 Skill
          </button>
          <input
            className="cf-search"
            aria-label="搜索技能"
            placeholder="搜索技能…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <div className="cf-items">
            {skills
              .filter((row) =>
                `${row.name} ${row.description}`
                  .toLowerCase()
                  .includes(search.trim().toLowerCase()),
              )
              .map((row) => (
                <div
                  className="cf-entry"
                  key={row.id}
                  data-selected={selected?.id === row.id}
                >
                  <button
                    className="cf-select"
                    disabled={busy}
                    aria-pressed={selected?.id === row.id}
                    onClick={() => void open(row)}
                  >
                    <span className="cf-avatar purple" aria-hidden="true">
                      ▤
                    </span>
                    <span>
                      <strong>{row.name}</strong>
                      <small>{row.description}</small>
                      <small>
                        v{row.number} · {row.enabled ? "已启用" : "已停用"}
                      </small>
                    </span>
                  </button>
                </div>
              ))}
          </div>
          {!skills.length && (
            <p className="cf-empty">还没有技能，从一个常用流程开始。</p>
          )}
          {!!skills.length &&
            !skills.some((row) =>
              `${row.name} ${row.description}`
                .toLowerCase()
                .includes(search.trim().toLowerCase()),
            ) && <p className="cf-empty">没有匹配的技能。</p>}
        </aside>
        <form
          id="skill-config-form"
          className="cf-editor"
          onSubmit={(event) => {
            event.preventDefault();
            if (!busy && canManage) void save();
          }}
        >
          <fieldset disabled={busy || !canManage} className="cf-stack">
            <section className="cf-card">
              <h2>基本信息</h2>
              <label>
                Skill 名称
                <input
                  required
                  maxLength={64}
                  pattern="[a-z0-9]+(-[a-z0-9]+)*"
                  placeholder="例如 weekly-report"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <p className="cf-help">
                小写英文、数字和连字符；描述和流程支持中文。
              </p>
              <label>
                技能描述
                <input
                  required
                  maxLength={500}
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
            </section>
            <section className="cf-card">
              <div className="cf-card-title">
                <h2>工作流程</h2>
                <span className="cf-tag">支持 Markdown</span>
              </div>
              <label>
                <span className="sr-only">技能流程（Markdown）</span>
                <textarea
                  aria-label="技能流程（Markdown）"
                  className="cf-workflow"
                  required
                  maxLength={6000}
                  rows={12}
                  value={instructions}
                  onChange={(event) => setInstructions(event.target.value)}
                />
              </label>
              <div className="cf-editor-foot">
                <small>技能说明会随版本保存，请勿填写密钥。</small>
                <small>{instructions.length} / 6000</small>
              </div>
            </section>
            <details className="cf-card cf-tool-range" ref={toolSection}>
              <summary>
                工具范围 <small>{tools.length} 项已授权</small>
              </summary>
              <p className="cf-help">
                实际可用工具仍受助手授权限制；不勾选表示禁用业务工具。
              </p>
              <div className="cf-tool-options">
                <ToolChoices
                  accountId={account.id}
                  value={tools}
                  onChange={setTools}
                  onCatalog={setCatalog}
                />
              </div>
            </details>
          </fieldset>
          {!canManage && (
            <p className="cf-help">
              你可以使用该技能；编辑权限属于创建者与空间管理员。
            </p>
          )}
        </form>
        <aside className="cf-summary-rail">
          <section className="cf-card">
            <h2>技能摘要</h2>
            <div className="cf-identity">
              <span className="cf-avatar purple" aria-hidden="true">
                ▤
              </span>
              <strong>{name || "未命名技能"}</strong>
              <span
                className="cf-status"
                data-enabled={selected?.enabled ?? enabled}
              >
                {selected ? (selected.enabled ? "已启用" : "已停用") : "未发布"}
              </span>
            </div>
            <dl className="cf-summary">
              <div>
                <dt>当前版本</dt>
                <dd>{selected ? `v${selected.number}` : "未发布"}</dd>
              </div>
              <div>
                <dt>工具范围</dt>
                <dd>{tools.length} 项</dd>
              </div>
              <div>
                <dt>使用范围</dt>
                <dd>当前工作空间</dd>
              </div>
            </dl>
            <div className="cf-divider">
              <h3>允许使用的工具</h3>
              <div className="cf-chips">
                {tools.length ? (
                  tools.map((tool) => (
                    <span key={tool}>
                      {catalog.find((item) => item.name === tool)?.title ||
                        tool}
                    </span>
                  ))
                ) : (
                  <small>未授权业务工具</small>
                )}
              </div>
              <p className="cf-help">实际可用工具仍受助手授权限制。</p>
              <button
                className="cf-link"
                onClick={() => {
                  if (toolSection.current) {
                    toolSection.current.open = true;
                    toolSection.current.scrollIntoView({
                      behavior: "smooth",
                      block: "center",
                    });
                  }
                }}
              >
                管理工具范围 →
              </button>
            </div>
          </section>
          <section className="cf-card">
            <h2>发布更新</h2>
            <p className="cf-help">
              发布后生成新版本，已有任务继续使用原版本。
            </p>
            <button
              className="cf-primary cf-wide"
              form="skill-config-form"
              disabled={busy || !canManage}
            >
              {busy ? "发布中…" : selected ? "发布新版本" : "创建 Skill"}
            </button>
            <label className="cf-switch-row">
              发布后启用
              <input
                type="checkbox"
                role="switch"
                checked={enabled}
                disabled={busy || !canManage}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              <span className="cf-switch-track" aria-hidden="true" />
            </label>
            {selected && canManage && (
              <div className="cf-direct-actions">
                <button
                  className="cf-link"
                  disabled={busy}
                  onClick={() => void changeState()}
                >
                  {selected.enabled ? "立即停用 Skill" : "立即启用 Skill"}
                </button>
                <button
                  className="cf-secondary cf-danger"
                  disabled={busy}
                  onClick={() => void changeState(true)}
                >
                  删除 Skill
                </button>
              </div>
            )}
          </section>
        </aside>
      </div>
    </section>
  );
}
