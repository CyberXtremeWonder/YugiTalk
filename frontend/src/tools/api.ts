export type ToolRole = "owner" | "admin" | "member";
export interface ToolParameter {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  description: string;
  required: boolean;
}
export interface ToolConfig {
  name: string;
  title: string;
  description: string;
  endpoint: string;
  method: "GET" | "POST";
  parameters: ToolParameter[];
  required_roles: ToolRole[];
  secret_name: string;
  timeout_seconds: number;
  max_response_bytes: number;
}
export interface ToolEntry {
  owner_id?: string | null;
  builtin: boolean;
  name: string;
  title: string;
  description: string;
  kind: "builtin" | "http";
  number: number;
  version_id?: string;
  enabled: boolean;
  ready: boolean;
  configuration_note: string;
  required_roles: ToolRole[];
  can_manage: boolean;
  schema: { parameters: Record<string, unknown> };
  config?: ToolConfig;
}

/** 从编辑器读取有限参数声明，详细类型和权限由服务端继续校验。 */
export function parseParameters(text: string): ToolParameter[] {
  const value: unknown = JSON.parse(text);
  if (!Array.isArray(value) || value.length > 12)
    throw new Error("参数定义应为最多 12 项的 JSON 数组。");
  return value as ToolParameter[];
}
