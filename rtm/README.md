# rtm/ — 需求追踪矩阵 (Requirements Traceability Matrix)

验证方法学的核心交付物：回答两个面试必问的问题——
**"你怎么证明每条需求都被测到了？"**（前向追踪）和
**"这条失败的影响哪条需求？"**（后向追踪）。

```
requirements.yaml   需求基线: 15 条 (FR-01~09 功能 / NFR-01~06 非功能),
                    每条含 title/description/priority/category/source
rtm_report.py       RTM 报告生成器 (纯 stdlib + PyYAML)
test_plan.md        测试计划: 范围/环境/分级/追踪方法/准入准出/风险
```

## 用法

```bash
# 1. 跑测试, 产出 junit 结果 (在仓库根目录)
.venv/bin/pytest tests/ --junitxml=report/junit.xml

# 2. 生成 RTM 报告 (Markdown + JSON 双输出)
.venv/bin/python rtm/rtm_report.py --junit report/junit.xml \
    [--coverage report/coverage_summary.json] -o report/rtm.md
```

参数均可缺省：不给 `--junit` 则只做静态覆盖分析（用例全部记 NOT_RUN），
不给 `--coverage` 则跳过覆盖率章节；`--tests-dir`、`--requirements`、
`--json-out` 同理有默认值。退出码：存在未覆盖需求时为 1，可直接接 CI 门禁。

## 数据流

```
requirements.yaml ──┐
tests/*.py (ast) ───┼─> 需求↔用例双向索引 ──> report/rtm.md  (人读)
report/junit.xml ───┘   + 执行结果回填    ──> report/rtm.json (机器读)
```

- 用例扫描用 **ast 静态解析** 而非 import：不执行任何测试代码，天然容忍
  并行开发中"部分用例存在/部分文件语法未稳"的中间态，也不会被缺失的
  固件或硬件依赖卡死。
- TC 编号从函数名 (`test_tc_boot_001_...`) 或 docstring 提取，需求 ID 从
  docstring 提取 (`FR-\d{2}` / `NFR-\d{2}`)，两处均做下划线/连字符归一化。
- junit 中有结果但源码扫不到的用例（改名残留）会如实列出；
  源码中有但没标需求 ID 的"孤儿用例"在报告 §4 单独点名。

## 方法学说明：为什么 RTM 是验证的基本工具

1. **覆盖可证伪**：没有 RTM，"测完了"只是感觉；有了 RTM，覆盖率 = 有用例
   的需求数 / 需求总数，缺口是一个可以数出来的清单。本项目准出标准要求
   需求覆盖率 100%（0 缺口）。
2. **变更影响分析**：需求变更时，RTM 反查"哪些用例受影响"；用例失败时，
   反查"哪条 must 级需求被阻塞"——两者都是硅后验证评审的例行问题。
3. **双向追踪是行业标准实践**：DO-178C、ISO 26262、IEC 62304 均强制要求
   需求↔验证项的双向追踪；RTM 是其最小公分母实现。
4. **自动化而非手工维护**：矩阵由脚本从需求基线 + 用例标注 + 执行结果三方
   数据自动生成，可进 CI，避免 Excel 矩阵与代码漂移。
5. **分级思维**：BOOT/FUNC/FLT/RCV/BM 五组对应"先活下来 → 正常功能 →
   异常注入 → 恢复能力 → 性能量化"的硅后验证典型次序，RTM 的分组统计
   让各层进度一目了然。

## 输出示例

报告主体是需求↔用例↔结果对照表，每行：需求 ID / 标题 / 优先级 / 覆盖
用例数 / 用例清单 / 各用例执行结果 / 状态（PASS / FAIL / 缺口 / 未执行）。
缺口章节列出零覆盖需求及其规格描述，直接指导下一步补用例。
