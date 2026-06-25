// Auto-generated Omnigent bridge extension for native Pi sessions.
const fs = require("fs");
const path = require("path");

function readConfig() {
  const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
  if (!configPath) return null;
  try {
    return JSON.parse(fs.readFileSync(configPath, "utf8"));
  } catch (_err) {
    return null;
  }
}

/**
 * Evaluate a TOOL_CALL policy for a native Pi tool via the Omnigent server's
 * session-level HTTP endpoint (POST /v1/sessions/{sessionId}/policies/evaluate).
 *
 * This is the same endpoint used by the Claude Code and Codex native hooks.
 * It does NOT require an active Omnigent turn context on the harness side —
 * the endpoint evaluates against the session's full policy set directly.
 * Fail-open (null) on any transport or parse error so a transient server
 * outage never wedges Pi mid-turn.
 */
async function evalNativePolicyHttp(config, toolName, args) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  )
    return null;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/policies/evaluate`;
  const body = JSON.stringify({
    event: {
      type: "PHASE_TOOL_CALL",
      target: "",
      data: { name: toolName, arguments: args },
      context: {},
    },
  });
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", ...(config.authHeaders || {}) },
      body,
    });
    if (!resp.ok) return null;
    const json = await resp.json();
    if (json.result === "POLICY_ACTION_DENY") {
      return { block: true, reason: json.reason || "blocked by Omnigent policy" };
    }
    return { block: false, reason: "" };
  } catch (_err) {
    // Keep Pi responsive if Omnigent is temporarily unavailable.
    return null;
  }
}

/**
 * Build a Pi tool-result object from an MCP ``tools/call`` JSON-RPC response.
 *
 * Pi expects ``{ content: [{ type: "text", text }], isError }``. The Omnigent
 * MCP proxy returns a JSON-RPC envelope whose ``result`` carries an MCP
 * content array (``[{ type: "text", text }]``) on success, or a JSON-RPC
 * ``error`` object (with the MCP convention code -32000 for tool denials /
 * tool errors) on failure. Map both into a single text block so the Pi agent
 * can read the output — denials surface as a readable error rather than
 * wedging the loop.
 */
function piResultFromMcpResponse(json) {
  if (json && typeof json === "object" && json.error) {
    const msg =
      (json.error && json.error.message) || "Omnigent tool call failed";
    return { content: [{ type: "text", text: String(msg) }], isError: true };
  }
  const result = json && typeof json === "object" ? json.result : undefined;
  // An ``input_required`` envelope must never reach this mapper: it carries no
  // ``content`` array, so it would otherwise fall to the "unexpected shape"
  // branch and be returned as ``isError: false`` — a confusing elicitation blob
  // masquerading as a successful tool result. callOmnigentTool detects and
  // resolves the ASK round-trip BEFORE calling this; treat a stray one as a
  // fail-closed error so an unresolved approval never reports success.
  if (result && typeof result === "object" && result.resultType === "input_required") {
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval that was not resolved",
        },
      ],
      isError: true,
    };
  }
  if (result && Array.isArray(result.content)) {
    const parts = [];
    for (const block of result.content) {
      if (block && typeof block === "object" && typeof block.text === "string") {
        parts.push(block.text);
      }
    }
    const text = parts.join("\n");
    return {
      content: [{ type: "text", text: text || safeJsonStringify(result) }],
      isError: result.isError === true,
    };
  }
  // Unexpected shape: hand the raw result back so nothing is silently dropped.
  return {
    content: [{ type: "text", text: safeJsonStringify(result ?? json ?? {}) }],
    isError: false,
  };
}

/**
 * Extract the elicitation id + opaque requestState from an MCP
 * ``input_required`` (MRTR) envelope, or ``null`` when the response is not an
 * ``input_required`` result.
 *
 * The Omnigent server keys ``inputRequests`` by the server-minted elicitation
 * id (the MRTR spec lets the client read those keys; only ``requestState`` is
 * opaque), so the first key is the id the retry must echo back inside
 * ``inputResponses``.
 */
function mcpInputRequired(json) {
  const result = json && typeof json === "object" ? json.result : undefined;
  if (!result || typeof result !== "object" || result.resultType !== "input_required") {
    return null;
  }
  const inputRequests =
    result.inputRequests && typeof result.inputRequests === "object"
      ? result.inputRequests
      : {};
  const elicitationId = Object.keys(inputRequests)[0] || "";
  const requestState =
    typeof result.requestState === "string" ? result.requestState : "";
  return { elicitationId, requestState };
}

/**
 * POST a single JSON-RPC ``tools/call`` to the server's per-session MCP proxy
 * and return the parsed response (or a fail-closed Pi tool-result on a
 * transport/HTTP error). ``extraParams`` carries the MRTR retry fields
 * (``requestState`` + ``inputResponses``) on the approval retry; it is omitted
 * on the initial call.
 *
 * @returns {Promise<{json: object} | {piResult: object}>} ``json`` on a parsed
 *   200 response; ``piResult`` is a terminal fail-closed tool result the caller
 *   returns as-is.
 */
async function postMcpToolsCall(config, toolName, args, rpcId, extraParams) {
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/mcp`;
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: rpcId,
    method: "tools/call",
    params: { name: toolName, arguments: args || {}, ...(extraParams || {}) },
  });
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(config.authHeaders || {}),
      },
      body,
    });
    if (!resp.ok) {
      return {
        piResult: {
          content: [
            {
              type: "text",
              text: `Omnigent tool call failed: HTTP ${resp.status}`,
            },
          ],
          isError: true,
        },
      };
    }
    return { json: await resp.json() };
  } catch (err) {
    return {
      piResult: {
        content: [
          {
            type: "text",
            text: `Omnigent tool call failed: ${err && err.message ? err.message : String(err)}`,
          },
        ],
        isError: true,
      },
    };
  }
}

/**
 * Execute an Omnigent tool by POSTing a JSON-RPC ``tools/call`` request to the
 * server's per-session MCP proxy endpoint
 * (``POST /v1/sessions/{sessionId}/mcp``).
 *
 * This is the SAME endpoint the runner's ``ProxyMcpManager`` uses: the Omnigent
 * server evaluates TOOL_CALL / TOOL_RESULT policy and then forwards execution
 * to the runner's ``/mcp/execute`` (which dispatches the real ``sys_*`` tool on
 * the correct machine with the session's terminal/workspace). The extension
 * already carries ``serverUrl`` + ``sessionId`` + ``authHeaders`` in its config,
 * so no extra relay process is needed.
 *
 * **ASK policy / elicitation round-trip (MRTR).** When the tool is gated by an
 * ASK policy the proxy returns HTTP 200 with an ``input_required`` result
 * (``{resultType, inputRequests, requestState}``) instead of executing. We must
 * NOT hand that envelope to the model as a result — it neither prompts nor runs
 * the tool. Mirroring ``ProxyMcpManager.dispatch()``, we resolve the human
 * verdict and retry once with the decision in ``inputResponses``:
 *   1. Long-poll ``POST /policies/evaluate`` via ``evalNativePolicyHttp`` — the
 *      SAME server-side ASK park the non-bridged hook uses. It holds the
 *      connection until a human resolves the approval card and collapses to a
 *      hard ALLOW / DENY (the extension has no in-process approval Future like
 *      the runner, so the long-poll IS its park/resolve mechanism).
 *   2. Retry the ``tools/call`` ONCE with ``requestState`` + ``inputResponses:
 *      {elicitationId: {action: "accept" | "decline"}}``. The server re-evaluates
 *      TOOL_CALL policy on the retry (it does not trust the client's claim
 *      blindly), so a still-denied tool stays denied.
 *   3. Cap at one retry; fail CLOSED (``isError: true``, readable message) if the
 *      approval cannot be resolved or the proxy still asks after the retry.
 *
 * (Trade-off: the proxy's ASK already published one approval card, and the
 * evaluate long-poll publishes a second one — the extension cannot re-attach to
 * the proxy-minted elicitation, whose id is in a different namespace. The human
 * resolves the evaluate card to drive the verdict; the proxy card is orphaned.
 * This is a UX wrinkle, not a security gap: the tool only runs on a genuine
 * human accept, and the server re-checks policy on the retry.)
 *
 * Fail-safe: any transport/parse error resolves to a readable tool-result error
 * (``isError: true``) rather than throwing, so a server hiccup never wedges Pi's
 * agent loop.
 */
async function callOmnigentTool(config, toolName, args) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  ) {
    return {
      content: [
        { type: "text", text: "Omnigent tool bridge is not configured" },
      ],
      isError: true,
    };
  }

  const first = await postMcpToolsCall(config, toolName, args, 1);
  if (first.piResult) return first.piResult;
  const initial = mcpInputRequired(first.json);
  if (initial === null) {
    // ALLOW / DENY / executed — map directly to a Pi tool result.
    return piResultFromMcpResponse(first.json);
  }

  // ── ASK: resolve the human verdict, then retry once (MRTR) ──────────
  if (!initial.elicitationId || !initial.requestState) {
    // Malformed input_required — cannot retry. Fail CLOSED.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval but the server sent no resolvable elicitation",
        },
      ],
      isError: true,
    };
  }

  const verdict = await evalNativePolicyHttp(config, toolName, args || {});
  if (verdict === null) {
    // The approval gate is structurally unavailable (no server/session/fetch,
    // or a transport error) — fail CLOSED rather than report false success.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call requires approval but the policy server was unreachable",
        },
      ],
      isError: true,
    };
  }
  const action = verdict.block ? "decline" : "accept";

  const retry = await postMcpToolsCall(config, toolName, args, 2, {
    requestState: initial.requestState,
    inputResponses: { [initial.elicitationId]: { action } },
  });
  if (retry.piResult) return retry.piResult;
  if (mcpInputRequired(retry.json) !== null) {
    // The proxy asked again after one approval round — do NOT loop. Fail CLOSED.
    return {
      content: [
        {
          type: "text",
          text: "Omnigent tool call still requires approval after one round — not retrying",
        },
      ],
      isError: true,
    };
  }
  return piResultFromMcpResponse(retry.json);
}

function textFromContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    const text =
      block.text || block.input_text || block.output_text || block.content;
    if (typeof text === "string") parts.push(text);
  }
  return parts.join("");
}

function textFromMessage(message) {
  if (!message || typeof message !== "object") return "";
  return textFromContent(
    message.content || message.parts || message.message || "",
  );
}

function safeJsonStringify(value) {
  try {
    return JSON.stringify(value ?? {});
  } catch (_err) {
    return String(value);
  }
}

function textFromToolResult(event) {
  if (!event || typeof event !== "object") return "";
  const text = textFromContent(event.content);
  if (text) return text;
  if ("result" in event) {
    const result = event.result;
    if (typeof result === "string") return result;
    if (result && typeof result === "object") {
      const resultText = textFromContent(result.content);
      if (resultText) return resultText;
    }
    return safeJsonStringify(result);
  }
  if ("details" in event) return safeJsonStringify(event.details);
  return "";
}

function contentBlocks(message) {
  if (
    !message ||
    typeof message !== "object" ||
    !Array.isArray(message.content)
  )
    return [];
  return message.content;
}

function fingerprint(text) {
  let hash = 5381;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  }
  return `${text.length}-${hash.toString(36)}`;
}

function messageRole(message) {
  if (!message || typeof message !== "object") return "";
  return String(message.role || message.type || "");
}

function headers(config) {
  return {
    "content-type": "application/json",
    ...(config.authHeaders || {}),
  };
}

async function postEvent(config, body) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/events`;
  try {
    await fetch(url, {
      method: "POST",
      headers: headers(config),
      body: JSON.stringify(body),
    });
  } catch (_err) {
    // Keep Pi responsive even if Omnigent is temporarily unavailable.
  }
}

async function patchExternalSessionId(config, nativeSessionId) {
  if (
    !nativeSessionId ||
    !config ||
    !config.serverUrl ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}`;
  try {
    await fetch(url, {
      method: "PATCH",
      headers: headers(config),
      body: JSON.stringify({ external_session_id: nativeSessionId }),
    });
  } catch (_err) {}
}

function setOmnigentStatus(config, ctx, state) {
  if (!ctx || !ctx.ui || !config) return;
  const urlLabel = config.conversationUrl
    ? `Omnigent: ${config.conversationUrl}`
    : "Omnigent";
  const label = state ? `${urlLabel} · ${state}` : urlLabel;
  try {
    ctx.ui.setTitle(`Omnigent: ${config.sessionId}`);
    ctx.ui.setStatus("omnigent", label);
    ctx.ui.setStatus("omnigent_state", undefined);
  } catch (_err) {}
}

function interruptActiveContext(ctx) {
  if (!ctx || typeof ctx.abort !== "function") return false;
  try {
    ctx.abort();
    return true;
  } catch (_err) {
    return false;
  }
}

function startInboxPoller(pi, config, handleInterrupt) {
  if (!config || !config.inboxDir || pi.__omnigentInboxPoller) return;
  // Bound the dedup set (FIFO eviction) — delivered files are unlinked, so a
  // long-lived TUI mustn't grow it unboundedly.
  const seen = new Set();
  const SEEN_CAP = 4096;
  const rememberSeen = (id) => {
    seen.add(id);
    while (seen.size > SEEN_CAP) seen.delete(seen.values().next().value);
  };
  // Cap send attempts so a persistently-failing sendUserMessage can't
  // re-read+re-throw the same file forever (the turn is already reported done).
  const deliverAttempts = new Map();
  const MAX_DELIVER_ATTEMPTS = 5;
  pi.__omnigentInboxPoller = setInterval(() => {
    let files = [];
    try {
      files = fs
        .readdirSync(config.inboxDir)
        .filter((name) => name.endsWith(".json"))
        .sort();
    } catch (_err) {
      return;
    }
    for (const file of files) {
      const fullPath = path.join(config.inboxDir, file);
      let payload;
      try {
        payload = JSON.parse(fs.readFileSync(fullPath, "utf8"));
      } catch (_err) {
        continue;
      }
      // Dedup only on a real string id; seen.has(undefined) would drop every
      // later id-less payload.
      const id = typeof payload?.id === "string" ? payload.id : null;
      if (!payload || (id !== null && seen.has(id))) {
        try {
          fs.unlinkSync(fullPath);
        } catch (_err) {}
        continue;
      }
      if (
        payload.type === "user_message" &&
        typeof payload.content === "string"
      ) {
        try {
          pi.sendUserMessage(payload.content, { deliverAs: "followUp" });
        } catch (_err) {
          // Leave the file to retry next tick, capped by attempt count.
          const key = id ?? fullPath;
          const attempts = (deliverAttempts.get(key) ?? 0) + 1;
          if (attempts < MAX_DELIVER_ATTEMPTS) {
            deliverAttempts.set(key, attempts);
            continue;
          }
          // Cap reached: surface the dropped follow-up without faking a turn
          // failure. The runner treats external_session_status:failed as
          // terminal for native sub-agents, so use a non-content conversation
          // error item and consume the file to stop the spin. Include the
          // message id and a short content preview so an operator can identify
          // what was lost (data loss; the file is unlinked below).
          deliverAttempts.delete(key);
          const droppedId = id ?? "(no id)";
          const preview =
            typeof payload.content === "string"
              ? payload.content.length > 80
                ? `${payload.content.slice(0, 80)}…`
                : payload.content
              : "";
          postEvent(config, {
            type: "external_conversation_item",
            data: {
              response_id: `pi-deliver-dropped-${Date.now()}`,
              item_type: "error",
              item_data: {
                source: "execution",
                code: "pi_followup_delivery_dropped",
                message:
                  `Omnigent: a queued follow-up message (id ${droppedId}) could ` +
                  `not be delivered to Pi after ${MAX_DELIVER_ATTEMPTS} attempts ` +
                  `and was dropped. Content preview: ${JSON.stringify(preview)}`,
              },
            },
          });
          try {
            fs.unlinkSync(fullPath);
          } catch (_err) {}
          continue;
        }
        deliverAttempts.delete(id ?? fullPath);
      }
      if (payload.type === "interrupt") {
        // An interrupt is point-in-time: make one delivery attempt, then
        // always consume the file (below). If there is no live turn to abort
        // right now, the interrupt is simply dropped — leaving the file would
        // re-read it every tick forever and, once a later turn creates an
        // abortable context, abort that unrelated turn. requestInterrupt only
        // arms the pendingInterrupt window when it catches a genuinely running
        // turn (idle interrupts are dropped, not armed — see F18), so a turn
        // already in flight still gets aborted via replay without poisoning the
        // next freshly-started turn.
        if (typeof handleInterrupt === "function") handleInterrupt();
      }
      if (id !== null) rememberSeen(id);
      try {
        fs.unlinkSync(fullPath);
      } catch (_err) {}
    }
  }, 250);
}

module.exports = function (pi) {
  const config = readConfig();
  let sequence = 0;
  let turnOrdinal = 0;
  let activeResponseId = null;
  // Dedicated loop-state flag, set on agent_start / cleared on agent_end. Used
  // as the no-isIdle() fallback for requestInterrupt instead of
  // !activeResponseId: agent_start resets activeResponseId to null and only
  // turn_start assigns it, so an interrupt landing in that gap (after
  // agent_start, before turn_start) would look idle by activeResponseId yet the
  // loop is genuinely running — agentRunning arms it correctly. See F18.
  let agentRunning = false;
  let latestContext = null;
  let pendingInterruptUntil = 0;
  const postedToolCalls = new Set();
  const postedToolResults = new Set();
  const postedReasoning = new Set();
  const toolCallsById = new Map();
  const pendingInterruptMs = 30_000;
  // Live streaming state for assistant text deltas. Pi emits
  // message_update events carrying an assistantMessageEvent of type
  // "text_delta" (token chunk) / "text_end" (block complete) during a
  // turn — see @earendil-works/pi-ai AssistantMessageEvent. We forward
  // each token as a transient external_output_text_delta so the web UI
  // paints a live preview before the final message lands.
  //
  // The preview is keyed by ASSISTANT MESSAGE, not per text block: the
  // web UI (chatStore.pumpStreamEvents) finalizes the OLDEST in-flight
  // "live:<message_id>" preview when the authoritative text_done arrives,
  // FIFO, and message_end posts ONE combined external_conversation_item
  // per assistant message (textFromMessage joins all text blocks). So a
  // 1:1 message-scoped id keeps exactly one preview per item; a per-block
  // id would orphan extra previews when a message has multiple text
  // blocks (e.g. text → tool call → more text). All text blocks of a
  // message share its id with a single monotonic chunk index, so the
  // preview reads as one growing message — matching claude-native.
  //
  // Deltas are best-effort live preview: postEvent fails open, and the
  // authoritative text still arrives via message_end regardless.
  //
  // streamingMessageOrdinal: bumped at each assistant message_end so the
  // NEXT message of the turn gets a fresh, stable id distinct from earlier
  // ones — see the message_end handler for why it advances there (not on
  // message_start) so deltas and the finalize agree on the id.
  let streamingMessageOrdinal = 0;
  // streamedTextIndex: message_id -> next 0-based chunk index.
  const streamedTextIndex = new Map();
  // finalizedTextBlocks: message_ids whose final delta was already posted,
  // so a duplicate text_end (or a stray text_delta after end) can't reopen
  // or double-finalize the preview.
  const finalizedTextBlocks = new Set();

  // Names of the Omnigent tools registered via pi.registerTool below. Bridged
  // tools are policy-evaluated server-side inside the /mcp proxy (TOOL_CALL +
  // TOOL_RESULT), so the tool_call hook must NOT also call
  // evalNativePolicyHttp for them — that would double-evaluate and, for ASK
  // policies, double-prompt. Pi's OWN built-in tools (read/shell/etc) are not
  // in this set and stay gated by the hook below.
  const bridgedTools = new Set();
  if (config && Array.isArray(config.tools)) {
    for (const tool of config.tools) {
      if (!tool || typeof tool !== "object") continue;
      const name = typeof tool.name === "string" ? tool.name : "";
      if (!name) continue;
      bridgedTools.add(name);
      const description =
        typeof tool.description === "string" ? tool.description : "";
      const parameters =
        tool.parameters && typeof tool.parameters === "object"
          ? tool.parameters
          : { type: "object", properties: {} };
      // Pi passes tool.parameters straight to the LLM as JSON Schema, so the
      // Omnigent schema is usable as-is. execute() round-trips the call to the
      // Omnigent server's MCP proxy and returns the result to Pi.
      if (typeof pi.registerTool === "function") {
        pi.registerTool({
          name,
          label: name,
          description,
          promptSnippet: description ? description.slice(0, 120) : name,
          parameters,
          async execute(_toolCallId, params) {
            return callOmnigentTool(config, name, params || {});
          },
        });
      }
    }
  }

  function rememberContext(ctx) {
    if (ctx) latestContext = ctx;
  }

  function newResponseId(prefix) {
    return `pi-${prefix}-${Date.now()}-${++sequence}`;
  }

  function currentResponseId() {
    if (!activeResponseId) activeResponseId = newResponseId("turn");
    return activeResponseId;
  }

  function hasPendingInterrupt() {
    if (!pendingInterruptUntil) return false;
    if (Date.now() > pendingInterruptUntil) {
      pendingInterruptUntil = 0;
      return false;
    }
    return true;
  }

  function safeIsIdle(ctx) {
    // Returns true/false from the SDK's isIdle(), or null when the signal is
    // unavailable (older SDK) or throws, so the caller can fall back.
    // Deliberately returns null (not true) on throw so callers fall back to loop
    // state (!agentRunning) rather than blindly treating the agent as idle.
    if (!ctx || typeof ctx.isIdle !== "function") return null;
    try {
      return ctx.isIdle();
    } catch (_err) {
      return null;
    }
  }

  function requestInterrupt(ctx) {
    // ctx.abort() is a silent no-op when the Pi agent is idle (it does NOT
    // throw), so an interrupt that arrives with no live turn must NOT arm the
    // replay window — otherwise the 30s window poisons the next legitimately
    // started turn (F18). Only arm when a turn is genuinely in-flight: prefer
    // the SDK's isIdle(), and fall back to the agent loop state on SDK versions
    // that don't expose it.
    const idle = safeIsIdle(ctx);
    const turnIsIdle = idle === null ? !agentRunning : idle;
    if (turnIsIdle) return false;
    const accepted = interruptActiveContext(ctx);
    if (!accepted) return false;
    pendingInterruptUntil = Date.now() + pendingInterruptMs;
    return true;
  }

  function replayPendingInterrupt(ctx) {
    if (!hasPendingInterrupt()) return false;
    interruptActiveContext(ctx);
    return true;
  }

  function clearPendingInterrupt() {
    pendingInterruptUntil = 0;
  }

  async function postToolCall(toolCall, responseId) {
    if (!toolCall || typeof toolCall !== "object") return;
    const callId = String(toolCall.id || toolCall.toolCallId || "");
    const name = String(toolCall.name || toolCall.toolName || "");
    if (!callId || !name) return;
    const key = `${responseId}:${callId}`;
    toolCallsById.set(callId, { key, responseId, name });
    if (postedToolCalls.has(key)) return;
    postedToolCalls.add(key);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "function_call",
        item_data: {
          agent: "Pi",
          name,
          arguments: safeJsonStringify(
            toolCall.arguments ?? toolCall.input ?? {},
          ),
          call_id: callId,
        },
      },
    });
  }

  async function postToolResult(event, responseId) {
    if (!event || typeof event !== "object") return;
    const callId = String(event.toolCallId || event.id || "");
    if (!callId) return;
    const known = toolCallsById.get(callId);
    const key = known && known.key ? known.key : `${responseId}:${callId}`;
    if (postedToolResults.has(key)) return;
    postedToolResults.add(key);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: known && known.responseId ? known.responseId : responseId,
        item_type: "function_call_output",
        item_data: {
          call_id: callId,
          output: textFromToolResult(event),
        },
      },
    });
  }

  async function postReasoningText(text, responseId, keyHint) {
    if (typeof text !== "string" || !text.trim()) return;
    const textKey = `${responseId}:text:${fingerprint(text)}`;
    const key = `${responseId}:${keyHint || fingerprint(text)}`;
    if (postedReasoning.has(key) || postedReasoning.has(textKey)) return;
    postedReasoning.add(key);
    postedReasoning.add(textKey);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "reasoning",
        item_data: {
          agent: "Pi",
          summary: [],
          content: [{ type: "reasoning_text", text }],
        },
      },
    });
  }

  function streamingMessageId(responseId) {
    // Stable per-assistant-message id across all of the message's text
    // chunks. The web UI keys an in-flight "live:<message_id>" preview off
    // this, appends each chunk in `index` order, and reconciles it against
    // the authoritative assistant item by FIFO retirement. responseId
    // scopes it to this turn and the ordinal distinguishes successive
    // assistant messages within the turn, so a finalized message's id is
    // never reused by a later one.
    return `${responseId}:msg:${streamingMessageOrdinal}`;
  }

  async function postOutputTextDelta(messageId, delta, options) {
    // Transient assistant-text chunk for live preview (Responses-style
    // response.output_text.delta on the wire). Not persisted; the
    // authoritative final text arrives separately via
    // external_conversation_item. A blank, non-final delta carries no
    // signal — skip it so an empty token can't churn the UI buffer.
    const final = !!(options && options.final);
    if (typeof delta !== "string") return;
    if (!delta && !final) return;
    const index = streamedTextIndex.get(messageId) || 0;
    streamedTextIndex.set(messageId, index + 1);
    await postEvent(config, {
      type: "external_output_text_delta",
      data: {
        delta,
        message_id: messageId,
        index,
        final,
      },
    });
  }

  async function postTextDelta(update, responseId) {
    // assistantMessageEvent of type "text_delta": one streamed token of
    // the current assistant message. All text blocks of the message share
    // its id, so the preview reads as one growing message.
    if (!update || typeof update.delta !== "string" || !update.delta) return;
    const messageId = streamingMessageId(responseId);
    if (finalizedTextBlocks.has(messageId)) return;
    await postOutputTextDelta(messageId, update.delta);
  }

  async function finalizeStreamingMessage(responseId) {
    // Emit a final-marker delta so the web UI knows no further chunks will
    // arrive for this message_id and can stop the live buffer. The marker
    // carries no new text (the running preview already holds the full
    // message); message_end posts the authoritative item that replaces the
    // preview in place. Only finalize a message we actually streamed (a
    // message with no text_delta has no live preview to close).
    const messageId = streamingMessageId(responseId);
    if (finalizedTextBlocks.has(messageId)) return;
    if (!streamedTextIndex.has(messageId)) return;
    finalizedTextBlocks.add(messageId);
    await postOutputTextDelta(messageId, "", { final: true });
  }

  async function mirrorAssistantMessage(message, responseId) {
    const blocks = contentBlocks(message);
    for (let index = 0; index < blocks.length; index += 1) {
      const block = blocks[index];
      if (!block || typeof block !== "object") continue;
      if (block.type === "toolCall") await postToolCall(block, responseId);
      if (block.type === "thinking") {
        const text = typeof block.thinking === "string" ? block.thinking : "";
        const key = block.thinkingSignature || `${turnOrdinal}:${index}`;
        await postReasoningText(text, responseId, key);
      }
    }
  }

  pi.registerCommand("omnigent", {
    description: "Show the Omnigent conversation URL",
    async handler(_args, ctx) {
      setOmnigentStatus(config, ctx, "linked");
      if (ctx && ctx.ui && config && config.conversationUrl) {
        ctx.ui.notify(`Omnigent: ${config.conversationUrl}`, "info");
      }
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    rememberContext(ctx);
    setOmnigentStatus(config, ctx, "linked");
    startInboxPoller(pi, config, () => requestInterrupt(latestContext));
    const nativeSessionId =
      ctx && ctx.sessionManager && ctx.sessionManager.getSessionId
        ? ctx.sessionManager.getSessionId()
        : undefined;
    await patchExternalSessionId(config, nativeSessionId);
    await postEvent(config, {
      type: "external_session_status",
      data: { status: "idle", response_id: `pi-${Date.now()}-${++sequence}` },
    });
  });

  pi.on("agent_start", async (_event, ctx) => {
    rememberContext(ctx);
    // A brand-new agent loop must never inherit a replay window armed before it
    // began (e.g. a spuriously-armed window from an interrupt that landed while
    // idle). A legitimate interrupt that arrives after this point belongs to
    // this loop and can still arm/replay; agent_end clears once the loop
    // completes. See F18.
    clearPendingInterrupt();
    agentRunning = true;
    setOmnigentStatus(config, ctx, "running");
    activeResponseId = null;
    turnOrdinal = 0;
    postedToolCalls.clear();
    postedToolResults.clear();
    postedReasoning.clear();
    toolCallsById.clear();
    streamedTextIndex.clear();
    finalizedTextBlocks.clear();
    streamingMessageOrdinal = 0;
    await postEvent(config, {
      type: "external_session_status",
      data: {
        status: "running",
        response_id: `pi-${Date.now()}-${++sequence}`,
      },
    });
  });

  pi.on("agent_end", async (_event, ctx) => {
    rememberContext(ctx);
    clearPendingInterrupt();
    agentRunning = false;
    setOmnigentStatus(config, ctx, "idle");
    activeResponseId = null;
    await postEvent(config, {
      type: "external_session_status",
      data: { status: "idle", response_id: `pi-${Date.now()}-${++sequence}` },
    });
  });

  pi.on("turn_start", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const index =
      event && typeof event.turnIndex === "number"
        ? event.turnIndex
        : turnOrdinal + 1;
    turnOrdinal = index;
    activeResponseId = newResponseId(`turn-${turnOrdinal}`);
  });

  pi.on("message_update", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    const update = event ? event.assistantMessageEvent : undefined;
    if (!update || typeof update !== "object") return;
    if (update.type === "text_delta") {
      await postTextDelta(update, responseId);
      return;
    }
    if (update.type === "toolcall_end") {
      await postToolCall(update.toolCall, responseId);
      return;
    }
    if (update.type === "thinking_end") {
      const key = `${turnOrdinal}:${update.contentIndex}`;
      await postReasoningText(update.content, responseId, key);
    }
  });

  pi.on("tool_execution_start", async (_event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
  });

  pi.on("tool_call", async (event, ctx) => {
    rememberContext(ctx);
    const blocked = replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await postToolCall(
      {
        id: event && event.toolCallId,
        name: event && event.toolName,
        arguments: event && event.input,
      },
      responseId,
    );
    if (blocked) {
      return { block: true, reason: "Interrupted by user" };
    }
    // Bridged Omnigent tools (registered via pi.registerTool above) are
    // policy-evaluated server-side inside the /mcp proxy when execute() runs,
    // so skip the hook-level eval for them to avoid double-evaluation and, for
    // ASK policies, a double prompt. Pi's own built-in tools (read/shell/etc)
    // are NOT bridged and stay gated here.
    if (bridgedTools.has((event && event.toolName) || "")) {
      return;
    }
    // Evaluate TOOL_CALL policy via the Omnigent server's session-level HTTP
    // endpoint. This works even after the harness turn has completed (which
    // happens immediately for pi-native — just enqueue + TurnComplete), so
    // the verdict is always evaluated against live session policies regardless
    // of whether an Omnigent turn is currently in flight.
    const verdict = await evalNativePolicyHttp(
      config,
      (event && event.toolName) || "",
      (event && event.input) || {},
    );
    if (verdict && verdict.block) {
      return { block: true, reason: verdict.reason || "blocked by Omnigent policy" };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    await postToolResult(event, currentResponseId());
  });

  pi.on("tool_execution_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    await postToolResult(event, currentResponseId());
  });

  pi.on("input", async (event, ctx) => {
    rememberContext(ctx);
    setOmnigentStatus(config, ctx, "running");
    const text = event && typeof event.text === "string" ? event.text : "";
    if (!text) return;
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: `pi-user-${Date.now()}-${++sequence}`,
        item_type: "message",
        item_data: {
          role: "user",
          content: [{ type: "input_text", text }],
        },
      },
    });
  });

  pi.on("message_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    setOmnigentStatus(config, ctx, undefined);
    const message = event ? event.message : undefined;
    const role = messageRole(message);
    if (role !== "assistant") return;
    const responseId = currentResponseId();
    // Close the live preview for this message (no-op if nothing streamed),
    // then bump the ordinal so the NEXT assistant message of this turn
    // streams under a fresh, distinct id and never reuses this one's. The
    // ordinal advances here (not on message_start) so the deltas just
    // posted and this finalize agree on the id regardless of whether Pi
    // fires message_start.
    await finalizeStreamingMessage(responseId);
    streamingMessageOrdinal += 1;
    await mirrorAssistantMessage(message, responseId);
    const text = textFromMessage(message);
    if (!text) return;
    // The authoritative assistant item. The web UI retires + replaces the
    // oldest in-flight live preview in place with this (FIFO; one preview
    // per message), so the streamed partials never duplicate the final.
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "message",
        item_data: {
          role: "assistant",
          agent: "Pi",
          content: [{ type: "output_text", text }],
        },
      },
    });
  });

  pi.on("turn_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await mirrorAssistantMessage(event && event.message, responseId);
    const results =
      event && Array.isArray(event.toolResults) ? event.toolResults : [];
    for (const result of results) {
      await postToolResult(result, responseId);
    }
  });
};
