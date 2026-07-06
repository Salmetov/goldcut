import { initData } from "./telegram";

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": initData(),
      ...(opts.headers ?? {}),
    },
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export type Quota = { used: number; limit: number | null; remaining: number | null; window_days: number };
export type Me = { id: number; username: string | null; plan: string; quota: Quota };
export type Settings = { mode: string; aspect_ratio: string; subtitles: boolean };
export type SettingsPatch = { aspect_ratio?: string; subtitles?: boolean; default_mode?: string };
export type Delivery = {
  id: number; title: string | null; source_url: string; start_s: number | null;
  end_s: number | null; mode: string | null; aspect_ratio: string | null;
  duration_s: number | null; created_at: string | null;
};

export const getMe = () => req<Me>("/api/me");
export const getSettings = () => req<Settings>("/api/settings");
export const putSettings = (s: SettingsPatch) =>
  req<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(s) });
export const getDeliveries = () => req<{ items: Delivery[] }>("/api/deliveries");
export const subscribe = () => req<{ invoice_url: string; price_xtr: number }>("/api/subscribe", { method: "POST" });
