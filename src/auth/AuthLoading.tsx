import { Spin } from '@arco-design/web-react';
import styles from './auth.module.less';

export default function AuthLoading() {
  return (
    <div className={styles.loading}>
      <Spin size={32} />
      <span>正在检查登录状态…</span>
    </div>
  );
}
