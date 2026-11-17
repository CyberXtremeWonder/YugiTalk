import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, test, vi } from "vitest";
import {
  fetchBalances,
  fetchUsage,
  type AccountBalance,
  type UsageSnapshot,
} from "./api";
import {
  balanceLabel,
  BalanceCard,
  usageLabel,
  UsageDetails,
  UsageView,
} from "./UsageView";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const snapshot: UsageSnapshot = {
  sampled_at: "2026-11-03T01:00:00Z",
  days: 30,
  summary: {
    model_calls: 3,
    input_tokens: 12000,
    output_tokens: 800,
    total_tokens: 12800,
    unknown_usage_calls: 1,
    unrecorded_runs: 2,
  },
  models: [
    {
      model: "test-grader",
      provider: "openai_compatible",
      model_calls: 3,
      input_tokens: 12000,
      output_tokens: 800,
      total_tokens: 12800,
      unknown_usage_calls: 1,
      cost: null,
      cost_status: "not_connected",
    },
  ],
  trend: [
    {
      date: "2026-11-03",
      input_tokens: 12000,
      output_tokens: 800,
      model_calls: 3,
    },
  ],
  balances: [],
  cost_status: "not_connected",
};

const account: AccountBalance = {
  id: "connection-1",
  label: "默认模型连接",
  provider: "deepseek",
  models: ["deepseek-flash", "test-grader"],
  status: "available",
  checked_at: "2026-11-03T01:01:00Z",
  is_available: true,
  amounts: [
    {
      currency: "CNY",
      total_balance: "1234.50",
      granted_balance: "34.50",
      topped_up_balance: "1200.00",
    },
  ],
};

test("用量与余额使用独立管理员接口、同源会话和取消信号", async () => {
  const fetch = vi.fn().mockImplementation(async () => new Response("{}"));
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await fetchUsage(7, signal);
  await fetchBalances(signal);
  expect(fetch.mock.calls[0][0]).toBe("/api/v1/admin/usage?days=7");
  expect(fetch.mock.calls[1][0]).toBe("/api/v1/admin/usage/balances");
  for (const [, options] of fetch.mock.calls) {
    expect(options.credentials).toBe("same-origin");
    expect(options.signal).toBe(signal);
    expect(options.method).toBe("GET");
  }
});

test("管理员权限失败不展示服务端原始内容", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response("private-provider-body", { status: 403 }),
      ),
  );
  await expect(
    fetchUsage(30, new AbortController().signal),
  ).rejects.toMatchObject({
    status: 403,
    message: "没有操作权限或请求校验失败。",
  });
});

test("零用量与未知用量明确区分，余额保留精度与币种", () => {
  expect(usageLabel(null)).toBe("未知");
  expect(usageLabel(0)).toBe("0");
  expect(usageLabel(12800)).toBe("12,800");
  expect(balanceLabel("0", "CNY")).toBe("¥0.00");
  expect(balanceLabel("1234.501234", "USD")).toBe("US$1,234.501234");
  expect(balanceLabel("9007199254740993.50", "CNY")).toBe(
    "¥9,007,199,254,740,993.50",
  );
});

test("部分用量和缺失费用不会被展示成完整消费或零费用", () => {
  const html = renderToStaticMarkup(<UsageDetails data={snapshot} />);
  expect(html).toContain("已知 Token 合计");
  expect(html).toContain("1 次请求用量不完整");
  expect(html).toContain("2 个任务未记录请求明细");
  expect(html).toContain("test-grader");
  expect(html).toContain("12,800");
  expect(html).toContain("费用接口未接入");
  expect(html).not.toContain("¥0");
  expect(html).not.toContain("NaN");
});

test("零请求有明确空状态，图表不会生成无效比例", () => {
  const html = renderToStaticMarkup(
    <UsageDetails
      data={{
        ...snapshot,
        summary: {
          model_calls: 0,
          input_tokens: 0,
          output_tokens: 0,
          total_tokens: 0,
          unknown_usage_calls: 0,
          unrecorded_runs: 0,
        },
        models: [],
        trend: [],
      }}
    />,
  );
  expect(html).toContain("所选期间还没有模型请求记录");
  expect(html).toContain("所选期间暂无模型用量");
  expect(html).not.toContain("NaN");
  expect(html).not.toContain("Infinity");
});

test("存在未知请求但已知用量为零时，图表峰值仍为零", () => {
  const html = renderToStaticMarkup(
    <UsageDetails
      data={{
        ...snapshot,
        summary: {
          ...snapshot.summary,
          input_tokens: 0,
          output_tokens: 0,
          total_tokens: 0,
        },
        trend: [
          {
            date: "2026-11-03",
            input_tokens: 0,
            output_tokens: 0,
            model_calls: 3,
          },
        ],
      }}
    />,
  );
  expect(html).toContain("峰值 0 Token / 天");
  expect(html).not.toContain("NaN");
});

test("同一账户的多个模型只呈现一次实际余额", () => {
  const html = renderToStaticMarkup(<BalanceCard balance={account} />);
  expect(html.match(/¥1,234.50/g)).toHaveLength(1);
  expect(html).toContain("deepseek-flash · test-grader");
  expect(html).toContain("充值余额");
  expect(html).toContain("赠送余额");
  expect(html).toContain("¥1,200.00");
});

test.each(["not_connected", "not_configured", "unavailable"] as const)(
  "余额状态 %s 不伪造零余额",
  (status) => {
    const html = renderToStaticMarkup(
      <BalanceCard
        balance={{ ...account, status, amounts: [], checked_at: null }}
      />,
    );
    expect(html).toContain("—");
    expect(html).not.toContain("¥0");
    expect(html).not.toContain("可用余额");
  },
);

test("真实零余额仍显示金额与供应商不足提示", () => {
  const html = renderToStaticMarkup(
    <BalanceCard
      balance={{
        ...account,
        is_available: false,
        amounts: [
          {
            currency: "USD",
            total_balance: "0",
            granted_balance: "0",
            topped_up_balance: "0",
          },
        ],
      }}
    />,
  );
  expect(html).toContain("US$0.00");
  expect(html).toContain("供应商提示余额不足");
});

test("初次进入页面分别呈现用量与余额加载状态", () => {
  const html = renderToStaticMarkup(<UsageView />);
  expect(html).toContain("Token 用量");
  expect(html).toContain("近 7 天");
  expect(html).toContain("近 30 天");
  expect(html).toContain("近 90 天");
  expect(html).toContain("正在加载模型用量");
  expect(html).toContain("正在查询账户余额");
  expect(html).toContain("账户共享余额，不按模型相加");
});
