/** 隐藏知识库回答的来源索引；流式半截标记暂不展示，避免逐字闪烁。 */
export function withoutSourceLabels(text: string, streaming = false): string {
  const clean = text.replace(/\[\s*S\d+\s*\]/g, "");
  return streaming ? clean.replace(/\[(?:\s*S\d*\s*)?$/, "") : clean;
}
