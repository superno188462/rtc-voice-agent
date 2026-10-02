import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { AIGC_PROXY_HOST } from '@/config';

export interface AuthUser {
  id: string;
  username: string;
  role: string;
  permissions: string[];
}

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

async function authRequest(path: string, body?: Record<string, string>): Promise<any> {
  const response = await fetch(`${AIGC_PROXY_HOST}${path}`, {
    method: body ? 'POST' : 'GET',
    credentials: 'include',
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = payload?.detail?.code || payload?.error?.code;
    const messages: Record<string, string> = {
      INVALID_CREDENTIALS: '用户名或密码错误',
      USERNAME_TAKEN: '这个用户名已经被注册',
      USERNAME_LENGTH: '用户名长度需要为 3 到 64 位',
      USERNAME_FORMAT: '用户名只能使用字母、数字和 . _ - @',
      PASSWORD_TOO_SHORT: '密码至少需要 8 位',
      PASSWORD_TOO_LONG: '密码不能超过 128 位',
      REGISTRATION_DISABLED: '当前暂不开放注册',
      AUTH_REQUIRED: '请先登录',
      LOGIN_RATE_LIMITED: '登录尝试过于频繁，请稍后再试',
    };
    throw new Error(messages[code] || '服务暂时不可用，请检查后端连接后重试');
  }
  return payload;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const result = await authRequest('/api/auth/me');
      setUser(result.user);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    await authRequest('/api/auth/login', { username, password });
    const result = await authRequest('/api/auth/me');
    setUser(result.user);
  }, []);

  const register = useCallback(async (username: string, password: string) => {
    await authRequest('/api/auth/register', { username, password });
    const result = await authRequest('/api/auth/me');
    setUser(result.user);
  }, []);

  const logout = useCallback(async () => {
    await authRequest('/api/auth/logout', {});
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, register, logout }),
    [user, loading, login, register, logout]
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
