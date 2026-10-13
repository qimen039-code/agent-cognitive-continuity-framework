---
name: capability-router
description: Use when currently exposed tools and skills cannot cover a concrete action, or a named capability is missing or fails. Find a bounded fallback while preserving the user's product and scope.
---

# Capability fallback lookup

The model selects the action from the user's goal, actual object, constraints, and side effects. If an exposed tool or skill covers it, use it directly without running a directory lookup.

1. Preserve the exact product, project, browser, account surface, and artifact format. Identify the missing action rather than searching the whole task narrative.
2. Search the current host's focused tool catalogue if available. Otherwise query the local directory once:

   ```powershell
   python -B scripts/capability_registry.py --response-profile projection --query "<missing action>" --anchor "<exact named surface>"
   ```

   Omit `--anchor` when none was named. Registration is local metadata, not current exposure. Current host paths and descriptions take precedence over cached plugin files.
3. Compare candidate action coverage, exposure, authentication, reliability, and cost. Select one primary skill for the action; add another for an independent missing capability. Method skills are optional support.
4. Read the selected current skill/schema. Filter `ALL_TOOLS` inside the runtime and emit at most eight short rows; load only the selected schema.
5. If absent, use a supported host refresh once for unchanged state. If none exists, report `host_refresh_required`. Do not invent callable tools or substitute another product.

Keep registered/configured, current_exposed, authenticated, callable, and action_covered separate. Unknown states remain unknown. Exact product matches and lexical scores identify candidates; semantic applicability and authorization remain with the model and host.

TTL and fingerprints support fallback lookup. Ordinary tasks need no scan, routing receipt, new rule for each phrasing, or repeated approval. Existing authorization governs the action.
