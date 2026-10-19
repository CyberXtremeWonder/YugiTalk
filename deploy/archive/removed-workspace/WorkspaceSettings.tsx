import { useEffect, useState, type FormEvent } from "react";
import { allItems, identityRequest, type Identity } from "../identity/api";
import { type Workspace } from "./api";

interface Member {
  id: string;
  email: string;
  name: string;
  role: Workspace["role"];
}

const roleNames = { owner: "所有者", admin: "空间管理员", member: "普通成员" };

/** 管理当前空间及成员角色，保护最后一位所有者。 */
export function WorkspaceSettings({
  identity,
  current,
  onRefresh,
}: {
  identity: Identity;
  current: Workspace | undefined;
  onRefresh: () => Promise<void>;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [spaceName, setSpaceName] = useState("");
  const [memberEmail, setMemberEmail] = useState("");
  const [memberRole, setMemberRole] = useState<"admin" | "member">("member");
  const [busy, setBusy] = useState(false);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [showDelete, setShowDelete] = useState(false);
  const [deleteSpaceId, setDeleteSpaceId] = useState("");
  const ownedSpaces = identity.workspaces.filter(
    (space) => space.role === "owner",
  );
  const deletionTarget =
    ownedSpaces.find((space) => space.id === deleteSpaceId) || ownedSpaces[0];
  const canManage = current?.role === "owner" || current?.role === "admin";
  /** 读取当前空间的有界成员列表，并更新管理页面。 */
  async function loadMembers() {
    if (!current || !canManage) return;
    const result = await allItems<Member>(`/workspaces/${current.id}/members`);
    setMembers(result);
  }
  useEffect(() => {
    // 切换空间后忽略上一空间的迟到响应，避免成员列表混入当前页面。
    let active = true;
    setMembers([]);
    setError("");
    setNotice("");
    setShowDelete(false);
    setDeleteConfirmation("");
    if (current && canManage)
      void allItems<Member>(`/workspaces/${current.id}/members`)
        .then((result) => {
          if (active) setMembers(result);
        })
        .catch((failure) => {
          if (active) setError(failure.message);
        });
    return () => {
      active = false;
    };
  }, [current?.id, current?.role, canManage]);
  /** 统一处理空间管理请求，成功后刷新身份与成员数据。 */
  async function operate(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice("操作已完成。");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }
  /** 使用校验后的名称创建空间，并同步工作台身份。 */
  function createSpace(event: FormEvent) {
    event.preventDefault();
    void operate(async () => {
      await identityRequest(
        "/workspaces",
        "POST",
        { name: spaceName },
        identity.csrf_token,
      );
      setSpaceName("");
      await onRefresh();
    });
  }
  /** 提交已初始化账号的邮箱与角色，成功后更新成员列表。 */
  function addMember(event: FormEvent) {
    event.preventDefault();
    if (!current) return;
    void operate(async () => {
      await identityRequest(
        `/workspaces/${current.id}/members`,
        "POST",
        { email: memberEmail, role: memberRole },
        identity.csrf_token,
      );
      setMemberEmail("");
      await loadMembers();
    });
  }
  /** 只有名称确认后才提交删除，成功后刷新并切换可用空间。 */
  function deleteSpace(event: FormEvent) {
    event.preventDefault();
    if (!deletionTarget || deleteConfirmation !== deletionTarget.name) return;
    void operate(async () => {
      await identityRequest(
        `/workspaces/${deletionTarget.id}`,
        "DELETE",
        undefined,
        identity.csrf_token,
      );
      setShowDelete(false);
      setDeleteConfirmation("");
      await onRefresh();
    });
  }
  return (
    <section className="workspace-settings" aria-label="工作空间管理">
      <h2>工作空间</h2>
      <p>
        {current
          ? `${current.name} · ${roleNames[current.role]}`
          : "尚未加入工作空间"}
      </p>
      <form onSubmit={createSpace}>
        <label htmlFor="space-name">新工作空间名称</label>
        <div className="form-row">
          <input
            id="space-name"
            maxLength={80}
            value={spaceName}
            onChange={(event) => setSpaceName(event.target.value)}
            required
            disabled={busy}
          />
          <button disabled={busy}>创建空间</button>
        </div>
      </form>
      {ownedSpaces.length > 0 && (
        <>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setDeleteSpaceId(
                current?.role === "owner" ? current.id : ownedSpaces[0].id,
              );
              setDeleteConfirmation("");
              setShowDelete(!showDelete);
            }}
          >
            删除工作空间
          </button>
          {showDelete && deletionTarget && (
            <form onSubmit={deleteSpace}>
              <label htmlFor="delete-space-select">选择要删除的个人空间</label>
              <select
                id="delete-space-select"
                value={deletionTarget.id}
                disabled={busy}
                onChange={(event) => {
                  setDeleteSpaceId(event.target.value);
                  setDeleteConfirmation("");
                }}
              >
                {ownedSpaces.map((space) => (
                  <option key={space.id} value={space.id}>
                    {space.name}
                  </option>
                ))}
              </select>
              <p>
                删除后所有成员将无法访问此空间，资料和审计记录保留。请先结束空间内任务。
              </p>
              <label htmlFor="delete-space-name">
                输入空间名称“{deletionTarget.name}”确认删除
              </label>
              <div className="form-row">
                <input
                  id="delete-space-name"
                  value={deleteConfirmation}
                  onChange={(event) =>
                    setDeleteConfirmation(event.target.value)
                  }
                  disabled={busy}
                />
                <button
                  disabled={busy || deleteConfirmation !== deletionTarget.name}
                >
                  确认删除
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setShowDelete(false)}
                >
                  取消
                </button>
              </div>
            </form>
          )}
        </>
      )}
      {canManage && current && (
        <>
          <h3>空间成员</h3>
          <p>已有成员无需重复添加；修改权限请使用下方成员列表。</p>
          <form onSubmit={addMember}>
            <label htmlFor="member-email">添加已初始化用户的邮箱</label>
            <div className="form-row">
              <input
                id="member-email"
                type="email"
                required
                value={memberEmail}
                onChange={(event) => setMemberEmail(event.target.value)}
                disabled={busy}
              />
              <select
                aria-label="新成员角色"
                value={memberRole}
                onChange={(event) =>
                  setMemberRole(event.target.value as "admin" | "member")
                }
                disabled={busy}
              >
                <option value="member">普通成员</option>
                <option value="admin">空间管理员</option>
              </select>
              <button disabled={busy}>添加成员</button>
            </div>
          </form>
          <ul className="member-list">
            {members.map((member) => (
              <li key={member.id}>
                <span>
                  {member.name}
                  <small>{member.email}</small>
                </span>
                <select
                  aria-label={`${member.email} 的角色`}
                  value={member.role}
                  disabled={
                    busy ||
                    member.id === identity.user.id ||
                    (member.role === "owner" && current.role !== "owner")
                  }
                  onChange={(event) => {
                    const role = event.target.value;
                    void operate(async () => {
                      await identityRequest(
                        `/workspaces/${current.id}/members/${member.id}`,
                        "PATCH",
                        { role },
                        identity.csrf_token,
                      );
                      await loadMembers();
                    });
                  }}
                >
                  <option value="member">普通成员</option>
                  <option value="admin">空间管理员</option>
                  {current.role === "owner" && (
                    <option value="owner">所有者</option>
                  )}
                </select>
                <button
                  disabled={
                    busy ||
                    member.id === identity.user.id ||
                    (member.role === "owner" && current.role !== "owner")
                  }
                  onClick={() => {
                    void operate(async () => {
                      await identityRequest(
                        `/workspaces/${current.id}/members/${member.id}`,
                        "DELETE",
                        undefined,
                        identity.csrf_token,
                      );
                      await loadMembers();
                    });
                  }}
                >
                  移除
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {error && (
        <>
          <p role="alert">{error}</p>
          {canManage && current && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void operate(loadMembers)}
            >
              重新加载成员
            </button>
          )}
        </>
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
