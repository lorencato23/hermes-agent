/**
 * Pure routing helpers for the contrib wiring controller.
 *
 * Kept out of wiring.tsx so they can be unit-tested without importing the whole
 * React/Electron controller module.
 */

import { knownSessionOwner } from '@/store/session'
import type { SessionOwnerScope, SessionProfileRoute } from '@/store/session-request-router'
import type { SessionInfo } from '@/types/hermes'

/**
 * The owner a contrib RPC dispatches on: the tile's exact route first (durable,
 * survives relaunch), else every sync source reconciled by `knownSessionOwner`.
 *
 * This is the store's `knownOwnerForSession` ladder with its inputs passed in
 * rather than read from atoms — the contrib dispatcher has already resolved the
 * runtime→stored id and read `$sessions` for this dispatch, and the explicit
 * arguments are what make the production path testable without standing up the
 * whole store.
 *
 * It deliberately returns `knownSessionOwner`'s full scope, ambiguous sentinel
 * included: reducing a connection-qualified owner to a bare profile name (what
 * `knownSessionProfile` returns) is the misroute — same-named profiles on two
 * connections both collapse onto the primary.
 */
export function resolveKnownSessionRpcOwner(
  sessions: readonly SessionInfo[],
  routingSessionId: null | string,
  tileOwner?: SessionProfileRoute
): SessionOwnerScope {
  return tileOwner ?? knownSessionOwner(sessions, routingSessionId)
}

/**
 * Resolve a runtime session id back to its stored id by reverse-scanning the
 * stored->runtime binding map — the same ladder use-session-tile-delegate's
 * `storedSessionIdForRuntime` uses. Returns undefined when the id isn't a known
 * runtime id, so the caller can treat it as already a stored id.
 */
export function findStoredIdForRuntimeId(bindings: Map<string, string>, runtimeId: string): string | undefined {
  for (const [storedId, mapped] of bindings) {
    if (mapped === runtimeId) {
      return storedId
    }
  }

  return undefined
}

/**
 * The stored session id a session-scoped RPC should route by.
 *
 * Route by the session the RPC TARGETS (its `session_id` param), not by the
 * window's focused tile: `requestGateway` is one shared closure for every
 * session RPC, so keying off the focused tile sent a non-focused tile's RPC
 * (a bot chat while another pane is active) to the focused tile's backend — the
 * Bot Mode misroute. `session_id` is a RUNTIME id while tiles/rows key on the
 * STORED id, so translate via the state cache, then the reverse binding scan;
 * an unknown id is already a stored id (several RPCs pass stored ids directly).
 * With no `session_id` at all (ambient/config calls) fall back to the focused
 * then selected tile.
 */
export function resolveRoutingSessionId(args: {
  paramSessionId: string | undefined
  storedIdForRuntime: (runtimeId: string) => string | undefined
  focusedStoredSessionId: null | string
  selectedStoredSessionId: null | string
}): null | string {
  const { focusedStoredSessionId, paramSessionId, selectedStoredSessionId, storedIdForRuntime } = args

  if (paramSessionId) {
    return storedIdForRuntime(paramSessionId) ?? paramSessionId
  }

  return focusedStoredSessionId ?? selectedStoredSessionId
}
