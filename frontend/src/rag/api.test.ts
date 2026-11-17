import { afterEach, expect, test, vi } from "vitest";
import { documentChunkLabel, uploadFiles, type UploadBatch } from "./api";

afterEach(() => vi.unstubAllGlobals());

test("展示实际识别结果并区分旧版资料", () => {
  expect(documentChunkLabel("pc-auto-short-v1")).toContain(
    "自动识别：通用短文档",
  );
  expect(documentChunkLabel("pc-long-long-v1")).toContain("长文档");
  expect(documentChunkLabel("leaf700-parent3-native-bm25-jieba-v2")).toBe(
    "原有 700 字符分块",
  );
});

/** 构造受控接口响应，隔离单元测试中的真实服务。 */
function response(id: string) {
  return new Response(
    JSON.stringify({
      id,
      filename: `${id}.txt`,
      status: "queued",
      progress: {},
    }),
    { status: 202 },
  );
}

test("文件夹递归列表逐个上传，跳过不支持文件，单文件失败继续", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(null, { status: 503 }))
    .mockResolvedValueOnce(response("two"));
  vi.stubGlobal("fetch", fetch);
  const first = new File(["a"], "first.md");
  Object.defineProperty(first, "webkitRelativePath", {
    value: "资料/子目录/first.md",
  });
  const progress: UploadBatch[] = [];
  const result = await uploadFiles(
    "base",
    [
      first,
      new File([], "empty.txt"),
      new File(["b"], "second.TXT"),
      new File(["x"], "image.svg"),
    ],
    "csrf",
    (batch) => progress.push(batch),
  );
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(result).toMatchObject({
    total: 3,
    completed: 3,
    uploaded: 1,
    failed: 2,
    skipped: 1,
    stopped: false,
  });
  expect(result.errors[0].name).toBe("资料/子目录/first.md");
  expect(progress[0].completed).toBe(0);
  expect(progress.at(-1)?.completed).toBe(3);
  expect(fetch.mock.calls[1][0]).toContain("filename=second.TXT");
});

test("权限失效后停止后续上传", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 403 }));
  vi.stubGlobal("fetch", fetch);
  const result = await uploadFiles(
    "base",
    [new File(["a"], "a.txt"), new File(["b"], "b.txt")],
    "csrf",
    () => {},
  );
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(result).toMatchObject({
    stopped: true,
    completed: 1,
    total: 2,
    failed: 1,
  });
});

test("分块策略冲突提示重新索引，不误报资料数量上限", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { code: "document_chunk_strategy_conflict" },
          }),
          { status: 409 },
        ),
      ),
  );
  const result = await uploadFiles(
    "base",
    [new File(["a"], "a.txt")],
    "csrf",
    () => {},
    "long",
  );
  expect(result.errors[0].message).toContain("重新索引");
  expect(result.errors[0].message).not.toContain("200");
});

test("自动识别与手动覆盖随每份文件上传，文件夹保持所选规则", async () => {
  const fetch = vi.fn().mockImplementation(async () => response("profile"));
  vi.stubGlobal("fetch", fetch);
  const files = [new File(["a"], "a.txt"), new File(["b"], "b.md")];
  await uploadFiles("base", files, "csrf", () => {}, "structured");
  expect(
    fetch.mock.calls.every(([url]) =>
      String(url).includes("chunk_strategy=structured"),
    ),
  ).toBe(true);
  await uploadFiles("base", [files[0]], "csrf", () => {});
  expect(fetch.mock.calls.at(-1)?.[0]).toContain("chunk_strategy=auto");
});

test("文件夹导入接受 JSONL、Excel、Word 和图片及大写扩展名", async () => {
  const fetch = vi.fn().mockImplementation(async () => response("office"));
  vi.stubGlobal("fetch", fetch);
  const result = await uploadFiles(
    "base",
    [
      "资料.xlsx",
      "资料.XLS",
      "记录.jsonl",
      "记录.JSONL",
      "说明.docx",
      "说明.DOC",
      "扫描.PNG",
      "照片.jpg",
    ].map((name) => new File(["office content"], name)),
    "csrf",
    () => {},
  );
  expect(fetch).toHaveBeenCalledTimes(8);
  expect(result).toMatchObject({ total: 8, uploaded: 8, skipped: 0 });
});

test("JSONL 接受 800 MB 和恰好 5 GB，超过上限拒绝，其他格式仍为 20 MB", async () => {
  const fetch = vi.fn().mockImplementation(async () => response("large"));
  vi.stubGlobal("fetch", fetch);
  const makeFile = (name: string, size: number) => {
    const file = new File(["x"], name);
    Object.defineProperty(file, "size", { value: size });
    return file;
  };
  const result = await uploadFiles(
    "base",
    [
      makeFile("800mb.jsonl", 800 * 1024 ** 2),
      makeFile("limit.JSONL", 5 * 1024 ** 3),
      makeFile("over.jsonl", 5 * 1024 ** 3 + 1),
      makeFile("large.pdf", 21 * 1024 ** 2),
    ],
    "csrf",
    () => {},
  );
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(result).toMatchObject({ uploaded: 2, failed: 2 });
  expect(result.errors[0].message).toContain("5 GB");
  expect(result.errors[1].message).toContain("20 MB");
});

test("超过批次上限或无支持文件时不发网络请求", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  await expect(
    uploadFiles(
      "base",
      Array.from({ length: 201 }, (_, i) => new File(["x"], `${i}.txt`)),
      "csrf",
      () => {},
    ),
  ).rejects.toThrow("200");
  await expect(
    uploadFiles("base", [new File(["x"], "a.exe")], "csrf", () => {}),
  ).resolves.toMatchObject({ total: 0, skipped: 1 });
  expect(fetch).not.toHaveBeenCalled();
});
