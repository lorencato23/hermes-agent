import { requestGatewayForAgent, requestGatewayForProfile } from '@/store/gateway'

export interface SessionProfileRoute {
  connectionId: string
  mode?: 'local' | 'remote'
  profile: string
  targetProfile?: string
}

export interface AmbiguousSessionOwner {
  ambiguous: true
}

/**
 * The one ambiguous-owner value. Owner resolution returns THIS object rather
 * than a fresh literal so the sentinel stays allocation-free on the per-RPC
 * dispatch path and callers may compare it by identity.
 */
export const AMBIGUOUS_SESSION_OWNER: AmbiguousSessionOwner = Object.freeze({ ambiguous: true })

export type SessionOwnerScope = undefined | null | string | AmbiguousSessionOwner | SessionProfileRoute

// ── Session-scoped RPC routing (the #89206 class) ───────────────────────────
// A session-scoped RPC (session.resume / session.activate / session.usage /
// prompt.submit) only means anything on the backend that OWNS the session's
// profile. A session's profile is a PROPERTY OF THE SESSION, not of whatever
// the window is currently showing. The "active gateway" is a moving target
// (a concurrent switch, an idle-reap eviction, a failed dial, or a connection
// edit re-points it) AND, for a hidden/unlisted session, it is simply the
// WRONG backend — one that never owned the session. Dispatching there 404s or
// times out while the session's own backend is healthy (blank Bot Chats, dead
// wake-ups; local pool and SSH alike).
//
// So: a KNOWN owner is always routed to its own profile's socket — there is no
// "same as active, so use ambient" shortcut, because "active" carries no
// routing authority. Only a genuinely UNKNOWN owner (a fresh draft with no
// session yet, or truly global chrome) falls to the ambient dispatcher, and
// callers are expected to resolve the owner (cross-profile probe) before they
// reach that case for a real session.

const normKey = (profile: null | string | undefined): string => (profile ?? '').trim() || 'default'

const isRoute = (owner: SessionOwnerScope): owner is SessionProfileRoute =>
  Boolean(owner && typeof owner === 'object' && 'connectionId' in owner)

/**
 * Contradictory owner evidence (two connections claim the same session, or a
 * profile projection disagrees with the connection-qualified owner). It is NOT
 * an unknown owner: unknown falls to ambient/probe, ambiguous must fail closed.
 */
export const isAmbiguousSessionOwner = (owner: SessionOwnerScope): owner is AmbiguousSessionOwner =>
  Boolean(owner && typeof owner === 'object' && 'ambiguous' in owner)

function routeParams(route: SessionProfileRoute, params: Record<string, unknown>): Record<string, unknown> {
  if (!route.targetProfile || !Object.prototype.hasOwnProperty.call(params, 'profile')) {
    return params
  }

  return { ...params, profile: route.targetProfile }
}

/**
 * True when a session-scoped RPC must be pinned to `ownerProfile`'s own socket.
 *
 * A KNOWN owner (route or profile name) always needs its own socket: the
 * session belongs to that profile regardless of what the window is showing.
 * There is deliberately NO comparison against the active profile — "active" is
 * presentation state, never a routing authority. Only a null/empty owner (a
 * fresh draft with no session, or global chrome) routes ambient.
 */
export function sessionRpcNeedsProfileRoute(ownerProfile: SessionOwnerScope | undefined): boolean {
  if (isAmbiguousSessionOwner(ownerProfile)) {
    // Ambiguous is never ambient: falling back to the active gateway is exactly
    // the misroute the sentinel exists to prevent. It is not routable either —
    // `requestForSessionProfile` rejects it before any dispatch — so this stays
    // an explicit branch rather than leaning on an object stringifying truthy.
    return true
  }

  if (isRoute(ownerProfile)) {
    // A descriptor is an immutable ownership claim. Even an explicitly local
    // route must not collapse to the ambient request: another connection can
    // expose the same profile name, and activation is UI state only.
    return Boolean(ownerProfile.connectionId.trim())
  }

  return ownerProfile != null && Boolean(String(ownerProfile).trim())
}

/**
 * Dispatch a session-scoped RPC on the socket that owns `ownerProfile`,
 * falling back to the ambient dispatcher when the active gateway already
 * serves that profile (keeps the primary's reauth-aware reconnect path).
 * The route is decided at CALL time, not at swap time.
 */
export function requestForSessionProfile<T>(
  ownerProfile: SessionOwnerScope | undefined,
  ambientRequest: <R>(
    method: string,
    params?: Record<string, unknown>,
    timeoutMs?: number,
    signal?: AbortSignal
  ) => Promise<R>,
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs?: number,
  signal?: AbortSignal
): Promise<T> {
  if (isAmbiguousSessionOwner(ownerProfile)) {
    // Name the method: an ambiguous owner surfaces to the user as a failed
    // resume/submit, and "which RPC" is the first thing a report needs.
    return Promise.reject(new Error(`Session owner is ambiguous; refusing to route session-scoped RPC (${method})`))
  }

  if (isRoute(ownerProfile)) {
    const connectionId = ownerProfile.connectionId.trim()

    if (!connectionId) {
      return Promise.reject(new Error('Session owner route is missing connectionId'))
    }

    const routedParams = routeParams(ownerProfile, params)

    return timeoutMs === undefined && signal === undefined
      ? requestGatewayForAgent<T>(connectionId, normKey(ownerProfile.profile), method, routedParams)
      : requestGatewayForAgent<T>(connectionId, normKey(ownerProfile.profile), method, routedParams, timeoutMs, signal)
  }

  if (!sessionRpcNeedsProfileRoute(ownerProfile)) {
    // Forward the extra args only when the caller actually supplied them. The
    // ambient dispatcher is a plain gateway request whose arity callers assert
    // on; handing it a trailing `undefined, undefined` on every session RPC
    // changes the observed call shape for the many callers that never asked
    // for a deadline (the plugin host bridge in contrib/wiring is the only one
    // that does).
    return timeoutMs === undefined && signal === undefined
      ? ambientRequest<T>(method, params)
      : ambientRequest<T>(method, params, timeoutMs, signal)
  }

  return requestGatewayForProfile<T>(normKey(ownerProfile), method, params, timeoutMs, signal)
}
