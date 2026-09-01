---
sidebar_position: 5
title: "内置技能目录"
description: "所有者分支已停用内置技能"
---

# 内置技能目录

此所有者分支不再随包提供任何内置技能，也不会把技能副本写入各 profile。

技能仅由各 profile 的 `skills.external_dirs` 配置提供。规范来源是 `novkien/hermes-skills`，profile 的技能策略不再由本仓库决定。

旧版 `hermes skills reset`、`opt-in` 和 `opt-out` 命令仅为兼容旧安装保留；它们无法从此分支恢复或重新写入内置技能。
