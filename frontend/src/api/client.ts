import type {
  TelegramLoginData,
  UserInfo,
  OverviewResponse,
  PositionResponse,
  TradeResponse,
  PerformanceResponse,
  UserSettingsResponse,
  UpdateSettingsRequest,
  WalletResponse,
  BotLoginStartResponse,
  BotLoginStatusResponse,
} from './types';

export const API_URL = import.meta.env.VITE_API_URL || (import.meta.env.PROD ? '' : 'http://localhost:8000');

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('Unauthorized');
  }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export const api = {
  loginTelegram: (data: TelegramLoginData) =>
    apiFetch<UserInfo>('/api/auth/telegram', { method: 'POST', body: JSON.stringify(data) }),
  getMe: () => apiFetch<UserInfo>('/api/auth/me'),
  logout: () => apiFetch<void>('/api/auth/logout', { method: 'POST' }),

  getOverview: () => apiFetch<OverviewResponse>('/api/overview'),

  getPositions: () => apiFetch<PositionResponse[]>('/api/positions'),
  closePosition: (id: number) =>
    apiFetch<{ success: boolean; tx_hash: string }>(`/api/positions/${id}/close`, { method: 'POST' }),

  getTrades: (page = 1, perPage = 20, sortBy = 'closed_at', sortOrder = 'desc') =>
    apiFetch<{ items: TradeResponse[]; page: number; per_page: number; total: number }>(
      `/api/trades?page=${page}&per_page=${perPage}&sort_by=${sortBy}&sort_order=${sortOrder}`,
    ),
  exportTrades: () => `${API_URL}/api/trades/export`,

  getPerformance: () => apiFetch<PerformanceResponse>('/api/performance'),

  getSettings: () => apiFetch<UserSettingsResponse>('/api/settings'),
  updateSettings: (data: Partial<UpdateSettingsRequest>) =>
    apiFetch<UserSettingsResponse>('/api/settings', { method: 'PUT', body: JSON.stringify(data) }),
  addBlacklist: (data: { token_address: string; chain?: string; reason?: string }) =>
    apiFetch<void>('/api/settings/blacklist', { method: 'POST', body: JSON.stringify(data) }),
  removeBlacklist: (address: string, chain = 'SOL', addedBy?: number) =>
    apiFetch<void>(`/api/settings/blacklist/${address}?chain=${chain}${addedBy != null ? `&added_by=${addedBy}` : ''}`, { method: 'DELETE' }),
  toggleAutoTrade: (enabled: boolean) =>
    apiFetch<void>('/api/settings/auto-trade', { method: 'PUT', body: JSON.stringify({ enabled }) }),

  getWallet: () => apiFetch<WalletResponse>('/api/wallet'),

  botLoginStart: () =>
    apiFetch<BotLoginStartResponse>('/api/auth/bot-login/start', { method: 'POST' }),
  botLoginStatus: (code: string) =>
    apiFetch<BotLoginStatusResponse>(`/api/auth/bot-login/status?code=${encodeURIComponent(code)}`),
};
