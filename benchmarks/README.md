# OmniSearch 质量基准

每个评估案例应提供人工确认的相关实体及其等价键（DOI、arXiv ID、仓库 ID、专利号
或规范化 URL 可以作为同一实体的别名），并记录预期的原始资料、代码和语言覆盖。评估时比较单源、普通多源、
多语言多源和完整递归研究四种模式。

核心指标由 `omnisearch.quality.evaluate_quality` 计算：Precision@K、Recall@K、来源与
语言多样性、重复率、原始资料比例和引用准确率。基准答案必须由人工维护，不能让
被测系统自行生成自己的正确答案。

运行内置的可复现冒烟基准：

```powershell
omnisearch benchmark benchmarks/smoke.json --output benchmark-report.json
```

可用 `--modes single_source multi_source multilingual` 跳过成本较高的完整研究模式。
正式评估时应复制 `smoke.json`，加入人工确认的领域案例及其稳定实体键；报告会保留每种
模式的结果键、Provider 失败信息、耗时和各项质量指标，便于回归比较。
