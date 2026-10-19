# MIRACL 中文训练集导入与基线评测

本轮使用用户已经下载的 `miracl/miracl` 中文训练集 Arrow 文件，不重新下载数据或执行远程数据集代码。

训练集共 1,312 个问题、3,187 份正例标注。将其所有正负例段落去重后，共 12,029 个段落，按原始 ID 排序，每 100 段组成一个 Markdown 分包，共 121 个文件。每段保留标题、`MIRACL_DOCID` 和原文。问题文本与正负标签只保存在评测文件，不导入知识库。

导入后产生 4,222 个叶子分块。知识库名称为 `MIRACL 中文训练集检索评测 20261016`，ID 为 `e6c758e2-0f72-4a08-868d-02f32017c17b`，归属现有 `backend/.env.rag-test` 配置中的评测账号。请使用该账号在开发页面查看。

## 文件位置

- `outputs/miracl-zh-train/import.json`：知识库与文件 ID、文件散列、索引状态，支持断点继续上传。
- `outputs/miracl-zh-train/documents/`：导入的原文分包。
- `outputs/miracl-zh-train/passage-files.json`：原始段落 ID 到分包文件的映射。
- `outputs/miracl-zh-train/cases.json`：全部训练问题、原始正例 ID、真实文档 ID 和标准引文。
- `outputs/miracl-zh-train/experiment.json`：本轮实际参数、硬件和备份位置。
- `outputs/miracl-zh-train/evaluation/`：逐题结果及评测摘要。

## 计分口径

对每道题，取 `/search` 最终返回的前五条证据，检查每份正例原文是否完整出现在一条返回证据中，忽略空白和英文大小写。仅命中分包文件不算命中该段落。Recall 为每题正例覆盖比例的宏平均；Hit 是至少覆盖一份正例；完整证据命中率要求该题所有正例均覆盖。

这是**训练集标注段落池上的完整原文覆盖检索基线**，不是官方 MIRACL 全量语料成绩，也不是独立测试集泛化成绩。原始段落被系统按 700 字符分块，跨块或截断可能造成严格完整匹配失败；其中 27 份正例标注原文长于 700 字符，1 份长于 1,900 字符。分包、标题和 ID 标记也使索引组织不同于官方逐段落检索设置。

本轮使用 BGE-M3 1024 维、Milvus Dense + 原生 BM25、RRF、父块合并及上下文截断。候选上限 24、Dense 阈值 0.35、上下文预算 5,000 字符、父块合并比例 0.5。图谱关闭，不运行问题分解、重排、Grader 或答案生成，不评价回答事实正确性或拒答质量。

## 复现

重新评测已有知识库：

```powershell
$env:PYTHONUTF8='1'
backend/.venv/Scripts/python.exe scripts/evaluate-knowledge-retrieval.py --cases outputs/miracl-zh-train/cases.json --base e6c758e2-0f72-4a08-868d-02f32017c17b --output outputs/miracl-zh-train/evaluation
```

图谱开关必须在实际服务端控制，而不是只在评测终端设置。`.cache/miracl-evaluation.compose.yml` 是本轮关闭图谱的开发服务覆盖配置。若常规服务已恢复图谱功能，复测前先明确此次是否要包含已构建的图谱。此知识库本轮不自动补建图谱，普通索引完成并不表示关系图已生成。

需要恢复中断的上传时，用原始 Arrow 文件运行：

```powershell
backend/.venv/Scripts/python.exe scripts/import-miracl-training.py --arrow "本地miracl-train.arrow完整路径"
```

脚本只操作 `import.json` 指定的新知识库；复用清单时不会再新建知识库，已记录 ID 的文件跳过上传。修改数据源散列或登录账号会被拒绝。`--prepare-only` 只导出分包与标注，不连接服务。
