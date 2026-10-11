import { FormEvent, useEffect, useState } from 'react';
import { Button, Input, Message } from '@arco-design/web-react';
import { IconLock, IconUser } from '@arco-design/web-react/icon';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import Logo from '@/assets/img/Logo.svg';
import { useAuth } from '@/auth/AuthContext';
import AuthLoading from '@/auth/AuthLoading';
import styles from './index.module.less';

export default function LoginPage() {
  const { user, loading, login, register } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [isRegister, setIsRegister] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const redirectTo = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname || '/';

  useEffect(() => {
    document.title = isRegister ? '创建账号 - 实时对话式 AI' : '登录 - 实时对话式 AI';
  }, [isRegister]);

  if (loading) return <AuthLoading />;
  if (user) return <Navigate to="/" replace />;

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    if (isRegister && password !== confirmPassword) {
      setError('两次输入的密码不一致');
      return;
    }
    setSubmitting(true);
    try {
      if (isRegister) await register(username.trim(), password);
      else await login(username.trim(), password);
      Message.success(isRegister ? '账号创建成功' : '登录成功');
      navigate(redirectTo, { replace: true });
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : '操作失败，请重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className={styles.page}>
      <section className={styles.card}>
        <div className={styles.brand}>
          <img src={Logo} alt="" />
          <span>实时对话式 AI 体验馆</span>
        </div>
        <h1>{isRegister ? '创建你的账号' : '登录语音助手'}</h1>
        <p className={styles.subtitle}>
          {isRegister ? '创建账号后即可开始语音对话' : '登录后继续你的语音对话体验'}
        </p>

        <form onSubmit={handleSubmit} className={styles.form}>
          <label htmlFor="username">用户名</label>
          <Input
            id="username"
            size="large"
            prefix={<IconUser />}
            placeholder="输入用户名"
            autoComplete="username"
            value={username}
            onChange={setUsername}
            minLength={3}
            maxLength={64}
            required
          />
          <label htmlFor="password">密码</label>
          <Input.Password
            id="password"
            size="large"
            prefix={<IconLock />}
            placeholder={isRegister ? '至少 8 位' : '输入密码'}
            autoComplete={isRegister ? 'new-password' : 'current-password'}
            value={password}
            onChange={setPassword}
            minLength={isRegister ? 8 : undefined}
            maxLength={128}
            required
          />
          {isRegister && (
            <>
              <label htmlFor="confirm-password">确认密码</label>
              <Input.Password
                id="confirm-password"
                size="large"
                prefix={<IconLock />}
                placeholder="再次输入密码"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={setConfirmPassword}
                maxLength={128}
                required
              />
            </>
          )}
          {error && <div className={styles.error} role="alert">{error}</div>}
          <Button
            htmlType="submit"
            type="primary"
            size="large"
            long
            loading={submitting}
            className={styles.submit}
          >
            {isRegister ? '创建账号' : '登录'}
          </Button>
        </form>

        <div className={styles.switchMode}>
          {isRegister ? '已经有账号？' : '还没有账号？'}
          <Button
            type="text"
            onClick={() => {
              setIsRegister((current) => !current);
              setError('');
            }}
          >
            {isRegister ? '返回登录' : '创建账号'}
          </Button>
        </div>
      </section>
      <footer className={styles.footer}>账号由身份中心统一管理</footer>
    </main>
  );
}
