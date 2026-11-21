import { useEffect, useState } from "react";

type Reply<T> = { key: string; value?: T; error?: unknown };

export function useResource<T>(
  key: string | null, load: (signal: AbortSignal) => Promise<T>, revision = 0, preserveValue = false,
) {
  const [reply, setReply] = useState<Reply<T>>();
  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    let active = true;
    setReply(previous => preserveValue && previous?.key === key && previous.value !== undefined ? previous : undefined);
    load(controller.signal).then(
      value => { if (active) setReply({ key, value }); },
      error => { if (active) setReply({ key, error }); },
    );
    return () => { active = false; controller.abort(); };
  // The key identifies the request and all of its inputs.
  }, [key, revision]);
  return reply?.key === key ? reply : undefined;
}

export function useLocal<T>(key: string, initial: T, isValue: (value: unknown) => value is T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      const parsed: unknown = raw === null ? null : JSON.parse(raw);
      return isValue(parsed) ? parsed : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Keep the current session usable. */ }
  }, [key, value]);
  return [value, setValue] as const;
}
