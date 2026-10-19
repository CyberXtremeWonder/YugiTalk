import { afterEach, expect, test, vi } from "vitest";
import { uploadFiles, type UploadBatch } from "./api";

afterEach(() => vi.unstubAllGlobals());

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
      new File(["x"], "image.jpg"),
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
