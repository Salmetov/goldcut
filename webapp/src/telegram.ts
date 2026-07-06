// Тонкая обёртка над Telegram WebApp SDK.

type WebApp = {
  initData: string;
  ready: () => void;
  expand: () => void;
  colorScheme: "light" | "dark";
  openInvoice: (url: string, cb: (status: string) => void) => void;
  HapticFeedback?: { notificationOccurred: (t: string) => void };
};

declare global {
  interface Window {
    Telegram?: { WebApp?: WebApp };
  }
}

export const tg = (): WebApp | undefined => window.Telegram?.WebApp;

export function initData(): string {
  // в браузере (dev вне Telegram) initData пуст — API вернёт 401, это ожидаемо
  return tg()?.initData ?? "";
}

export function openInvoice(url: string): Promise<string> {
  return new Promise((resolve) => {
    const w = tg();
    if (!w) return resolve("unsupported");
    w.openInvoice(url, resolve);
  });
}
