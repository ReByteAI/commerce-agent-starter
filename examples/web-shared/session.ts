// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentApi } from "./api";

export interface Session {
  /** Null while login is in flight or after it failed. */
  sessionId: string | null;
  /** The signed-in operator's name, on merchant sessions. */
  operator?: string;
  /** The signed-in shopper, on storefront sessions. */
  shopper?: { name: string; tier?: string };
  /** Replaces an expired server session if it still belongs to this profile. */
  renewSession: (expiredSessionId: string) => Promise<boolean>;
}

interface SessionState {
  profile?: string;
  sessionId: string | null;
  operator?: string;
  shopper?: { name: string; tier?: string };
}

interface Renewal {
  generation: number;
  expiredSessionId: string;
  promise: Promise<boolean>;
}

const generations = new WeakMap<AgentApi, number>();

/**
 * Only the newest start installs its token. Key the caller on the profile so per-session
 * state resets with it.
 */
export function useSession(
  api: AgentApi,
  options: { profile?: string } = {},
): Session {
  const { profile } = options;
  const [session, setSession] = useState<SessionState>({ profile, sessionId: null });
  const generationRef = useRef<number | null>(null);
  const renewal = useRef<Renewal | null>(null);
  const recovered = useRef<{
    generation: number;
    expiredSessionId: string;
    sessionId: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const generation = (generations.get(api) ?? 0) + 1;
    generations.set(api, generation);
    generationRef.current = generation;
    api.session = null;
    recovered.current = null;
    setSession({ profile, sessionId: null });
    void (async () => {
      const started = await api.startSession(profile ? { user_id: profile } : undefined);
      if (
        cancelled ||
        generationRef.current !== generation ||
        generations.get(api) !== generation
      ) {
        return;
      }
      api.session = started?.sessionId ?? null;
      setSession({
        profile,
        sessionId: started?.sessionId ?? null,
        operator: started?.operator,
        shopper: started?.shopper,
      });
    })();
    return () => {
      cancelled = true;
      if (generationRef.current === generation) generationRef.current = null;
      if (generations.get(api) === generation) {
        generations.set(api, generation + 1);
        api.session = null;
      }
    };
  }, [api, profile]);

  const renewSession = useCallback(
    (expiredSessionId: string): Promise<boolean> => {
      const generation = generationRef.current;
      if (generation === null || generations.get(api) !== generation) {
        return Promise.resolve(false);
      }

      if (api.session !== expiredSessionId) {
        const prior = recovered.current;
        return Promise.resolve(
          prior?.generation === generation &&
            prior.expiredSessionId === expiredSessionId &&
            prior.sessionId === api.session,
        );
      }

      const pending = renewal.current;
      if (
        pending?.generation === generation &&
        pending.expiredSessionId === expiredSessionId
      ) {
        return pending.promise;
      }

      const promise = (async () => {
        const started = await api.startSession(profile ? { user_id: profile } : undefined);
        if (
          !started ||
          generationRef.current !== generation ||
          generations.get(api) !== generation ||
          api.session !== expiredSessionId
        ) {
          return false;
        }
        api.session = started.sessionId;
        recovered.current = {
          generation,
          expiredSessionId,
          sessionId: started.sessionId,
        };
        setSession({
          profile,
          sessionId: started.sessionId,
          operator: started.operator,
          shopper: started.shopper,
        });
        return true;
      })().finally(() => {
        if (renewal.current?.promise === promise) renewal.current = null;
      });
      renewal.current = { generation, expiredSessionId, promise };
      return promise;
    },
    [api, profile],
  );

  return session.profile === profile
    ? {
        sessionId: session.sessionId,
        operator: session.operator,
        shopper: session.shopper,
        renewSession,
      }
    : { sessionId: null, renewSession };
}
