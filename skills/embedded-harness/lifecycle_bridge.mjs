// @ts-check

import { spawn } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import { basename, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * @typedef {"SessionStart"|"PreCompact"|"PostCompact"|"SessionEnd"|"UserPromptSubmit"} HookEventName
 * @typedef {{hook_event_name?: HookEventName, session_id?: string, thread_id?: string, transcript_path?: string, cwd?: string, prompt?: string, [key: string]: unknown}} HookInput
 * @typedef {{additionalContext?: string}} HookOutput
 * @typedef {{registryPath?: string, maxContextChars?: number, pythonExecutable?: string, ledgerScript?: string, checkpointTimeoutMs?: number}} BridgeOptions
 */

const PROJECTION_EVENTS = new Set(["SessionStart", "PostCompact"]);
const CHECKPOINT_EVENTS = new Set(["PreCompact", "SessionEnd"]);
const KNOWN_EVENTS = new Set([...PROJECTION_EVENTS, ...CHECKPOINT_EVENTS, "UserPromptSubmit"]);
const POINTER_SCHEMA = "cbh.ledger_generation_pointer.v1";
const MANIFEST_SCHEMA = "cbh.ledger_generation_manifest.v1";
const DEFAULT_REGISTRY = "C:\\skill-tools\\codex-embedded-harness\\active_conversation_memory_registry.json";
const DEFAULT_LEDGER_SCRIPT = fileURLToPath(new URL("./codex_session_ledger.py", import.meta.url));

/** @param {string} path */
async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

/** @param {string} value */
function normalizedPath(value) {
  return resolve(value).replaceAll("/", "\\").toLocaleLowerCase("en-US");
}

/** @param {string} child @param {string} parent */
function isInside(child, parent) {
  const childPath = normalizedPath(child);
  const parentPath = normalizedPath(parent).replace(/[\\/]+$/, "");
  return childPath === parentPath || childPath.startsWith(parentPath + "\\");
}

/** @param {HookInput} input */
function inputSessionId(input) {
  const direct = String(input.session_id ?? input.thread_id ?? "").trim();
  if (direct) return direct;
  const transcript = String(input.transcript_path ?? "");
  const match = transcript.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  return match?.[0] ?? "";
}

/** @param {HookInput} input @param {string} registryPath */
async function resolveActiveEntry(input, registryPath) {
  const registry = await readJson(registryPath);
  const entries = Array.isArray(registry.entries) ? registry.entries : [];
  const active = entries.filter(
    (entry) =>
      entry &&
      typeof entry === "object" &&
      String(entry.state ?? "").toUpperCase() === "ACTIVE" &&
      entry.chain_head === true,
  );
  const sessionId = inputSessionId(input);
  if (sessionId) {
    const exact = active.filter((entry) => String(entry.thread_id ?? "") === sessionId);
    if (exact.length === 1) return exact[0];
    if (exact.length > 1) return undefined;
  }
  const cwd = String(input.cwd ?? "").trim();
  if (!cwd) return undefined;
  const workspaceMatches = active.filter((entry) => {
    const workspace = String(entry.workspace_path ?? "").trim();
    return workspace && isInside(cwd, workspace);
  });
  return workspaceMatches.length === 1 ? workspaceMatches[0] : undefined;
}

/** @param {string} ledgerRoot */
async function activeGeneration(ledgerRoot) {
  const root = resolve(ledgerRoot);
  const pointer = await readJson(join(root, "CURRENT.json"));
  if (pointer.schema !== POINTER_SCHEMA || typeof pointer.relative_path !== "string") {
    throw new Error("ledger_pointer_invalid");
  }
  const generation = resolve(root, pointer.relative_path);
  const outside = relative(root, generation);
  if (outside.startsWith("..") || isAbsolute(outside)) {
    throw new Error("ledger_pointer_outside_root");
  }
  const manifest = await readJson(join(generation, "generation.json"));
  if (manifest.schema !== MANIFEST_SCHEMA) {
    throw new Error("ledger_manifest_invalid");
  }
  return { root, generation, pointer, manifest };
}

/** @param {string} value */
function quoted(value) {
  return JSON.stringify(value);
}

/** @param {Record<string, any>} entry @param {HookInput} input @param {number} limit */
async function memoryHandle(entry, input, limit) {
  const ledgerRoot = String(entry.ledger_root_path ?? "");
  const { generation, pointer, manifest } = await activeGeneration(ledgerRoot);
  const sessionId = inputSessionId(input);
  const sessions = Array.isArray(manifest.sessions) ? manifest.sessions : [];
  const checkpoint =
    sessions.find((item) => String(item.session_id ?? "") === sessionId) ?? sessions[0];
  if (!checkpoint) throw new Error("ledger_checkpoint_missing");
  const transcript = String(input.transcript_path ?? checkpoint.raw_session_path ?? "");
  const currentSize = transcript ? (await stat(transcript)).size : Number(checkpoint.checkpoint_offset_bytes ?? 0);
  const checkpointOffset = Number(checkpoint.checkpoint_offset_bytes ?? 0);
  const rawTail = currentSize > checkpointOffset ? "yes" : "no";
  const memoryRoot = String(entry.root_path ?? "");
  const metaPath = String(entry.meta_path ?? join(memoryRoot, "_META_INDEX.md"));
  const statePath = join(memoryRoot, "conversation_state.md");
  const linksPath = join(memoryRoot, "memory_links.jsonl");
  const ledgerIndex = join(generation, "_LEDGER_INDEX.md");
  const core =
    `CBH memory handle (navigation only; model decides relevance; raw is canonical): ` +
    `memory_id=${String(entry.memory_id ?? "")} state=ACTIVE generation=${String(pointer.generation_id ?? basename(generation))} ` +
    `checkpoint_line=${Number(checkpoint.checkpoint_line_count ?? 0)} raw_tail=${rawTail} ` +
    `meta=${quoted(metaPath)} ledger=${quoted(ledgerIndex)}`;
  const expanded = `${core} state_path=${quoted(statePath)} links=${quoted(linksPath)}`;
  const selected = expanded.length <= limit ? expanded : core;
  return selected.length <= limit ? selected : selected.slice(0, Math.max(0, limit));
}

/** @param {string} command @param {string[]} args @param {number} timeoutMs */
function runProcess(command, args, timeoutMs) {
  return new Promise((resolvePromise) => {
    const child = spawn(command, args, {
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill(), timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      if (stdout.length < 64_000) stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      if (stderr.length < 16_000) stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      resolvePromise({ status: null, stdout, stderr, error });
    });
    child.on("close", (status) => {
      clearTimeout(timer);
      resolvePromise({ status, stdout, stderr });
    });
  });
}

/** @param {Record<string, any>} entry @param {HookInput} input @param {BridgeOptions} options */
async function checkpointLedger(entry, input, options) {
  const transcript = String(input.transcript_path ?? "");
  const ledgerRoot = String(entry.ledger_root_path ?? "");
  if (!transcript || !ledgerRoot) return;
  const python = options.pythonExecutable ?? process.env.CBH_PYTHON ?? "python";
  const script = options.ledgerScript ?? process.env.CBH_LEDGER_SCRIPT ?? DEFAULT_LEDGER_SCRIPT;
  const args = [
    "-B",
    script,
    "generation-refresh",
    "--session-jsonl",
    transcript,
    "--output-dir",
    ledgerRoot,
    "--project-lane",
    String(entry.project_lane ?? "PROJECTLESS"),
    "--title",
    String(entry.title ?? "Conversation ledger"),
    "--conversation-memory-id",
    String(entry.memory_id ?? ""),
    "--conversation-memory-path",
    String(entry.root_path ?? ""),
  ];
  const result = await runProcess(
    python,
    args,
    Number(options.checkpointTimeoutMs ?? process.env.CBH_CHECKPOINT_TIMEOUT_MS ?? 20_000),
  );
  if (result.status !== 0) return;
  try {
    JSON.parse(result.stdout);
  } catch {
    return;
  }
}

/**
 * @param {HookInput} input
 * @param {BridgeOptions} [options]
 * @returns {Promise<HookOutput|undefined>}
 */
export async function handleLifecycleEvent(input, options = {}) {
  try {
    if (!input || typeof input !== "object") return undefined;
    const eventName = String(input.hook_event_name ?? "");
    if (!KNOWN_EVENTS.has(eventName)) return undefined;
    if (eventName === "UserPromptSubmit") return undefined;
    const registryPath =
      options.registryPath ??
      process.env.CBH_ACTIVE_CONVERSATION_REGISTRY ??
      DEFAULT_REGISTRY;
    const entry = await resolveActiveEntry(input, registryPath);
    if (!entry) return undefined;
    if (PROJECTION_EVENTS.has(eventName)) {
      const additionalContext = await memoryHandle(
        entry,
        input,
        Number(options.maxContextChars ?? process.env.CBH_CONTEXT_LIMIT ?? 800),
      );
      return additionalContext ? { additionalContext } : undefined;
    }
    if (CHECKPOINT_EVENTS.has(eventName)) {
      await checkpointLedger(entry, input, options);
    }
    return undefined;
  } catch {
    return undefined;
  }
}

/** @returns {Promise<number>} */
export async function runCli() {
  try {
    let raw = "";
    process.stdin.setEncoding("utf8");
    for await (const chunk of process.stdin) raw += chunk;
    if (!raw.trim()) return 0;
    const input = JSON.parse(raw);
    const output = await handleLifecycleEvent(input);
    if (output) process.stdout.write(JSON.stringify(output));
  } catch {
    return 0;
  }
  return 0;
}

const currentFile = fileURLToPath(import.meta.url);
if (process.argv[1] && resolve(process.argv[1]) === currentFile) {
  process.exitCode = await runCli();
}
