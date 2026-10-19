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
  const [notice, setNotice] = useState("");
  const alive = useRef(true);
  const path = `/me/skills`;
  const canManage = !selected || selected.can_manage;
  /** 刷新技能摘要目录，已删除条目由服务端过滤。 */
  async function refresh() {
    const rows = await allItems<Skill>(path);
    if (alive.current) setSkills(rows);
  }
  useEffect(() => {
    alive.current = true;
    void refresh().catch((error) => {
      if (alive.current) setNotice(error.message);
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
      if (!alive.current) return;
      setSelected(detail);
      setName(detail.name);
      setDescription(detail.description);
      setInstructions(detail.instructions || "");
      setTools(detail.allowed_tools);
      setEnabled(detail.enabled);
    } catch (error) {
      if (alive.current)
        setNotice(error instanceof Error ? error.message : "读取技能失败");
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
        setNotice(error instanceof Error ? error.message : "保存技能失败");
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
        setNotice(error instanceof Error ? error.message : "更新技能失败");
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  return (
    <section className="secondary-view" aria-labelledby="view-title">
      <h1 id="view-title">Skill</h1>
      <p>
        把常用的工作流程保存为技能，在对话中按需使用。技能在当前工作账号内使用。
      </p>
      <div className="agent-layout">
        <div className="agent-list skill-list">
          <button disabled={busy} onClick={reset}>
            ＋ 新 Skill
          </button>
          {!skills.length && <p>尚无自定义 Skill</p>}
          {skills.map((row) => (
            <button
              key={row.id}
              disabled={busy}
              aria-pressed={selected?.id === row.id}
              onClick={() => void open(row)}
            >
              <strong>{row.name}</strong>
              <small>
                v{row.number} · {row.enabled ? "已启用" : "已停用"}
              </small>
              <small>{row.description}</small>
            </button>
          ))}
        </div>
        <form
          className="account-settings agent-form skill-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <fieldset disabled={busy || !canManage}>
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
            <p className="field-hint">
              使用小写英文、数字和连字符；描述和流程支持中文。
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
            <label>
              技能流程（Markdown）
              <textarea
                aria-label="技能流程（Markdown）"
                required
                maxLength={6000}
                rows={12}
                value={instructions}
                onChange={(event) => setInstructions(event.target.value)}
              />
            </label>
            <p className="field-hint">
              {instructions.length} / 6000
              字。填写流程与输出要求，请勿保存密钥。
            </p>
            <fieldset>
              <legend>可使用的只读工具</legend>
              <ToolChoices
                accountId={account.id}
                value={tools}
                onChange={setTools}
              />{" "}
            </fieldset>
            <label className="tool-checkbox">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              发布后启用
            </label>
          </fieldset>
          <p className="field-hint">
            工具必须同时被 Agent 授权；不勾选表示禁用业务工具。Skill
            不执行自定义脚本，也不能绕过知识库的证据要求。
          </p>
          {!canManage && (
            <p>你可以使用该技能；编辑权限属于创建者与空间管理员。</p>
          )}
          {notice && <p role="status">{notice}</p>}
          <div className="skill-actions">
            <button disabled={busy || !canManage} type="submit">
              {selected ? "发布新版本" : "创建 Skill"}
            </button>
            {selected && canManage && (
              <>
                <button
                  disabled={busy}
                  type="button"
                  onClick={() => void changeState()}
                >
                  {selected.enabled ? "停用 Skill" : "启用 Skill"}
                </button>
                <button
                  disabled={busy}
                  type="button"
                  onClick={() => void changeState(true)}
                >
                  删除 Skill
                </button>
              </>
            )}
          </div>
        </form>
      </div>
    </section>
  );
}
