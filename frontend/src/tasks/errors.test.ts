import { expect, test } from "vitest";
import { runError } from "./errors";

test("评分故障区分网络、鉴权、格式和截止时间并提供中文恢复提示", () => {
  expect(runError("model_timeout_or_network")).toContain("连接超时");
  expect(runError("model_authentication")).toContain("访问权限");
  expect(runError("rag_structured_output_invalid")).toContain("评分模型");
  expect(runError("rag_deadline")).toContain("缩小问题范围");
});

test("未知错误不回显任意供应商内容，保留已有输出上限提示", () => {
  expect(runError("private-provider-content")).not.toContain(
    "private-provider",
  );
  expect(runError("model_output_limit")).toContain("发布新版本");
});

test("区分协议流、实际正文大小和模型输出 Token 上限", () => {
  expect(runError("model_stream_size_limit")).toContain("流容量配置");
  expect(runError("model_response_too_large")).toContain("正文或工具参数");
  expect(runError("model_output_limit")).toContain("Token 上限");
});
