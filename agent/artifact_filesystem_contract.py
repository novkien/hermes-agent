"""Universal filesystem discipline injected into every Hermes agent prompt."""

ARTIFACT_FILESYSTEM_CONTRACT = """## Artifact Filesystem Contract

Treat every file write as a lifecycle decision, not merely a pathname choice.
1. Before writing, classify the file as canonical project content, temporary work, retained artifact, human report, final export, durable state, cache, trash, or workflow-declared remote canonical content.
2. Destination precedence is: exact owner/task destination; active skill or workflow contract; nearest project/repository instructions; this contract; task-scoped temporary fallback.
3. A lower layer may specialize a destination but never weaken read-only, no-mutation, ownership, preservation, secret, or task-scope constraints.
4. Keep source, tests, maintained documentation, configuration, fixtures, and declared project outputs in their canonical project tree when authorized. Do not copy project source into the Hermes workspace merely to satisfy this contract.
5. Follow the project's declared build, distribution, generated-output, and cache directories. Do not reorganize a project without explicit authority.
6. Put scratch files, downloads, debug dumps, intermediate data, generated patches, ad hoc repros, and disposable builds in a task-scoped temporary namespace, never loose in a workspace, project, repository, or system root.
7. Namespace non-trivial temporary and retained work by scope plus task or run ID. Do not mix independent runs in one directory.
8. Use `artifacts/` for retained machine-readable evidence, logs, manifests, collected data, checksums, and reproducibility bundles; use `reports/` for human-readable reports; use `exports/` for final consumer deliverables.
9. Use `state/` only for mutable data required by a component to continue operating. Use `cache/` only for data that can be regenerated.
10. Respect exact workflow destinations for remote canonical content. A remote path is not canonical merely because a tool returned it.
11. Never infer a canonical project, repository, source tree, build root, or live runtime root from `~/.hermes/workspace/remote-data/**`, a remote/network mount, a storage-host path, or a generic memory/infrastructure inventory. Such locations are storage or transport surfaces unless the owner, exact task, active workflow, or nearest project contract explicitly names that exact remote location as canonical.
12. A remembered workspace inventory is not project-location authority. When project placement is unresolved, use the active domain/project placement contract or preserve the result in task-scoped temporary storage instead of selecting a storage path by convention.
13. Stage disposable work first, verify the intended output, then promote only verified material to its canonical destination.
14. Preserve original inputs unless overwrite is explicitly authorized. Never convert an edit request into destructive replacement by default.
15. Clean disposable temporary material after successful promotion. If residue must remain, report its exact path and retention reason.
16. Move uncertain untracked workspace material to dated `trash/` when reversible disposal is required. Use repository/project semantics for tracked files; never move tracked source into Hermes trash.
17. Do not change ignore files merely to hide files created by the current task. Add shared ignore rules only when they are a genuine project convention.
18. Multi-file consumer deliverables require a containing directory and a concise manifest or README unless the active workflow defines another package contract.
19. At completion, return exact retained paths, artifact classes, canonical project mutations, cleanup status, and any intentional residue. Never claim a file, upload, promotion, or cleanup without direct evidence.
20. If no authoritative permanent destination exists, keep the result in the task-scoped temporary namespace and report that unresolved destination instead of inventing one."""


__all__ = ["ARTIFACT_FILESYSTEM_CONTRACT"]