---
sidebar_position: 5
title: "Bundled Skills Catalog"
description: "Bundled skills are retired in the owner fork"
---

# Bundled Skills Catalog

This owner fork intentionally ships no bundled skills and does not seed profile-local skill copies.

Skills are supplied by owner-managed registries through each profile's `skills.external_dirs` configuration. The canonical registry is `novkien/hermes-skills`; profile skill policy belongs there rather than in this repository.

The legacy `hermes skills reset`, `opt-in`, and `opt-out` commands remain compatibility operations for old installations; they cannot restore or seed a bundled skill from this fork.
