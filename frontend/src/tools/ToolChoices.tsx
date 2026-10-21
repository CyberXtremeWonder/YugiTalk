import { useEffect, useState } from "react";
import { allItems } from "../identity/api";
import type { ToolEntry } from "./api";

/** 从空间目录读取授权选项，已删除的旧授权仍可取消，配置未就绪不等于获得调用权。 */
export function ToolChoices({
  accountId,
  value,
  onChange,
}: {
  accountId: string;
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const [catalog, setCatalog] = useState<ToolEntry[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void allItems<ToolEntry>(`/me/tools`)
      .then((rows) => {
        if (active) setCatalog(rows);
      })
      .catch((failure) => {
        if (active) setError(failure.message);
      });
    return () => {
      active = false;
    };
  }, [accountId]);
  const choices = [
    // 目录项之外还保留草稿中的旧名称，让用户能主动撤销已删除工具的授权。
    ...catalog.map((item) => ({
      name: item.name,
      title:
        item.title +
        (!item.enabled ? "（已停用）" : !item.ready ? "（待配置）" : ""),
    })),
    ...value
      .filter((name) => !catalog.some((item) => item.name === name))
      .map((name) => ({ name, title: `${name}（目录中不可用，可取消）` })),
  ];
  return (
    <>
      {choices.map(({ name, title }) => (
        <label className="tool-checkbox" key={name}>
          <input
            type="checkbox"
            checked={value.includes(name)}
            disabled={!value.includes(name) && value.length >= 12}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...value, name]
                  : value.filter((item) => item !== name),
              )
            }
          />
          {title}
        </label>
      ))}
      {error && <p role="alert">{error}</p>}
      <p className="field-hint">
        最多授权 12 项；实际调用仍需工具启用、配置就绪和角色允许。
      </p>
    </>
  );
}
