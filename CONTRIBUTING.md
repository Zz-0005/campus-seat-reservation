# 协作约定

本项目按真实软件团队的流程推进：需求 → 开发 → 用户测试 → 问题修复 → 版本发布。

## 分支模型

| 分支 | 用途 |
| --- | --- |
| `main` | 可交付主线，只接受经过评审的 PR；每次发布在这里打 tag |
| `feature/*` | 新功能开发，例如 `feature/seat-booking-core` |
| `fix/*` | 缺陷修复，例如 `fix/poll-when-hidden` |
| `docs/*` | 文档修订，例如 `docs/prd-v1.1` |

## 提交流程

1. 每个需求或问题对应一个分支，分支名体现改动内容；
2. 提交信息用 `类型: 说明` 的形式，类型取 `feat` / `fix` / `docs` / `test` / `refactor` / `chore`；
3. 开 PR 时在描述里写清「改了什么 / 为什么改 / 怎么验证」，并关联对应 Issue（`Fixes #12`）；
4. 合并前必须通过 `python tests/test_booking.py`；
5. 合并后再打 tag 发布 Release，Release Notes 写清楚新增功能与修复的问题。

## Issue 处理流程

`Issue Submitted → Triage → Reproduce → Fix → Code Review → Test → User Acceptance → Closed`

- 提交者：按模板写清问题描述、复现步骤、预期结果、实际结果、截图或日志；
- 产品：判断问题类型与优先级，补上标签；
- 研发：在 Issue 里公开回复核验结论，说明原因与修复方式；
- 用户：按修复后的版本重新验证，通过后关闭。

## 角色

| 角色 | 职责 |
| --- | --- |
| 产品经理（PM） | 需求调研、PRD、优先级、验收 |
| 用户（User A / B） | 提出真实需求、试用产品、提交 Issue、验收 |
| 研发（Developer） | 技术方案、AI 辅助编码、测试、PR 与发布 |
