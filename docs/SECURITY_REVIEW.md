# 阶段6镜像安全处置记录

日期：2026-10-11。当前安全门槛未通过。扫描来自本地Trivy漏洞库，扫描容器无网络、无Docker socket，保留完整原始报告。运行依赖扫描和实际镜像扫描分开，不能用前者的通过覆盖基础系统或编译进二进制的漏洞。

部署镜像记录见[全部镜像报告](./stages/artifacts/stage6-all-images-audit.json)。当前Milvus 2.5.27仍有2项严重漏洞，PostgreSQL镜像有4项严重记录；其余高危项也须处置。正式默认不部署旧MinIO，本地测试替身的漏洞单独标记。

## 候选镜像评估

两个候选均来自官方仓库，未部署、未迁移现有向量或对象数据。记录见[候选报告](./stages/artifacts/stage6-candidate-images-audit.json)。

本轮收尾已删除这两个未采用的Docker候选及临时镜像导出，保留JSON扫描记录。当前部署仍使用报告中的2.5.27，不能把删除候选当成运行版本漏洞已经修复。[清理记录](./stages/artifacts/stage6-image-cleanup.json)、[此前候选清理](./stages/artifacts/stage6-candidate-cleanup.json)。

| Milvus候选 | 固定摘要 | 严重 / 高危 | 结论 |
| --- | --- | --- | --- |
| 2.6.23 | `60e96d5abe7ea9199dd8a4d0d0506f9b57a7056cf57683327b8c7ae7f489ed17` | 0 / 92 | 不作为安全通过候选 |
| 2.6.24 | `c55810ca95c97437cc003f913b835524600fb95a14ce04955a92517c3ccab73f` | 0 / 15 | 严重项减少，但仍有高危，且需升级/恢复兼容性演练 |

当前部署中的Milvus严重项位于编译后的`milvus/bin/milvus`：gRPC的CVE-2026-33186、Go标准库的CVE-2025-68121。只更新操作系统包不会替换这些内嵌依赖。2.6.23的高危项主要也位于Milvus二进制及planparser共享库，不能把一次`apt upgrade`当成修复证明。

[官方2.6分支说明](https://milvus.io/docs/v2.6.x/release_notes.md)列出2.6.24及对应SDK；[独立部署升级说明](https://milvus.io/docs/v2.6.x/upgrade_milvus_standalone-docker.md)要求按升级过程处理数据。下一步须取得已修复依赖的候选镜像并复扫，再从一致快照复制到独立目录验证认证、旧资料召回、删除、重建索引和恢复。原数据与原镜像保持可恢复，不直接用新版本打开唯一的数据副本。

PostgreSQL同主版本`16-bookworm`重新拉取仍是已扫描摘要`0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4`，没有因此取得修复。它的严重记录涉及SQLite、libxml2、zlib及gosu内嵌Go标准库；不同系统发行版的切换还需考虑排序规则和数据恢复，不能只换标签。

## 区分扫描记录和实际影响

Debian对[CVE-2023-45853](https://security-tracker.debian.org/tracker/CVE-2023-45853)说明，Bookworm的zlib不构建涉事contrib/minizip代码。该说明是进一步核对镜像实际安装包与构建产物的依据；此处未自动忽略报告，也未将整个PostgreSQL镜像标记为通过。[SQLite记录](https://security-tracker.debian.org/tracker/CVE-2025-7458)和[libxml2记录](https://security-tracker.debian.org/tracker/CVE-2026-6653)仍需分别判断修复、可达性与防护条件。网络隔离只能降低部分暴露，不能自动消除漏洞。

每项最终处置应保存：镜像ID、包/二进制路径、CVE、修复版本或维护方判断、实际构建证据、复扫结果、兼容性验收、责任人与复核期限。目前没有风险豁免，没有把未修复项改成零漏洞。
