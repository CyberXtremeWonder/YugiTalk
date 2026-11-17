import { identityRequest } from "../identity/api";

export type UsageDays = 7 | 30 | 90;

export interface UsageTotals {
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  unknown_usage_calls: number;
}

export interface ModelUsage extends UsageTotals {
  provider: string;
  model: string;
  cost: null;
  cost_status: "not_connected";
}

export interface UsageSnapshot {
  sampled_at: string;
  days: UsageDays;
  timezone?: string;
  summary: UsageTotals & { unrecorded_runs: number };
  models: ModelUsage[];
  trend: {
    date: string;
    input_tokens: number;
    output_tokens: number;
    model_calls: number;
  }[];
  balances: AccountBalance[];
  cost_status: "not_connected";
}

export interface AccountBalance {
  id: string;
  label: string;
  provider: string;
  models: string[];
  status: "available" | "not_connected" | "not_configured" | "unavailable";
  checked_at: string | null;
  is_available: boolean | null;
  amounts: {
    currency: "CNY" | "USD";
    total_balance: string;
    granted_balance: string;
    topped_up_balance: string;
  }[];
}

export interface BalanceSnapshot {
  sampled_at: string;
  balances: AccountBalance[];
  truncated: boolean;
}

/** 按时间窗口读取实际模型请求用量，费用缺失不转换为零。 */
export function fetchUsage(days: UsageDays, signal: AbortSignal) {
  return identityRequest<UsageSnapshot>(
    `/admin/usage?days=${days}`,
    "GET",
    undefined,
    undefined,
    undefined,
    signal,
  );
}

/** 账户余额独立读取，供应商查询不会阻塞本地用量明细。 */
export function fetchBalances(signal: AbortSignal) {
  return identityRequest<BalanceSnapshot>(
    "/admin/usage/balances",
    "GET",
    undefined,
    undefined,
    undefined,
    signal,
  );
}
