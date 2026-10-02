/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { Button, Divider, Message, Popover } from '@arco-design/web-react';
import { IconMenu } from '@arco-design/web-react/icon';
import { useNavigate } from 'react-router-dom';
import { useSelector } from 'react-redux';
import NetworkIndicator from '@/components/NetworkIndicator';
import { useIsMobile } from '@/utils/utils';
import { useAuth } from '@/auth/AuthContext';
import { useLeave } from '@/lib/useCommon';
import { RootState } from '@/store';
import Logo from '@/assets/img/Logo.svg';
import styles from './index.module.less';

const Disclaimer = 'https://www.volcengine.com/docs/6348/68916';
const ReversoContext = 'https://www.volcengine.com/docs/6348/68918';
const UserAgreement = 'https://www.volcengine.com/docs/6348/128955';

interface HeaderProps {
  children?: React.ReactNode;
  hide?: boolean;
}

function Header(props: HeaderProps) {
  const { children, hide } = props;
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const leaveRoom = useLeave();
  const isJoined = useSelector((state: RootState) => state.room.isJoined);
  const handleLogout = async () => {
    try {
      if (isJoined) await leaveRoom();
      await logout();
      navigate('/login', { replace: true });
    } catch {
      Message.error('退出失败，请检查后端连接后重试');
    }
  };

  const MenuProps = [
    {
      name: '免责声明',
      url: Disclaimer,
    },
    {
      name: '隐私政策',
      url: ReversoContext,
    },
    {
      name: '用户协议',
      url: UserAgreement,
    },
  ];

  return (
    <div
      className={styles.header}
      style={{
        display: hide ? 'none' : 'flex',
      }}
    >
      <div className={styles['header-logo']}>
        {useIsMobile() ? null : (
          <Popover
            content={
              <div className={styles['menu-wrapper']}>
                {MenuProps.map((menuItem) => (
                  <Button
                    type="text"
                    key={menuItem.name}
                    onClick={() => {
                      window.open(menuItem.url, '_blank');
                    }}
                  >
                    {menuItem.name}
                  </Button>
                ))}
              </div>
            }
          >
            <IconMenu className={styles['header-setting-btn']} />
          </Popover>
        )}
        <img src={Logo} alt="Logo" />
        <Divider type="vertical" />
        <span className={styles['header-logo-text']}>实时对话式 AI 体验馆</span>
        <NetworkIndicator />
      </div>
      {children}
      {useIsMobile() ? (
        user ? (
          <Button type="text" size="mini" className={styles.mobileLogout} onClick={handleLogout}>
            退出
          </Button>
        ) : null
      ) : (
        <div className={styles['header-right']}>
          {user && (
            <>
              <span className={styles.accountName}>{user.username}</span>
              <Button type="text" className={styles['header-right-text']} onClick={handleLogout}>
                退出登录
              </Button>
            </>
          )}
          <div
            className={styles['header-right-text']}
            onClick={() =>
              window.open('https://www.volcengine.com/product/veRTC/ConversationalAI', '_blank')
            }
          >
            官网链接
          </div>
          <div
            className={styles['header-right-text']}
            onClick={() =>
              window.open(
                'https://www.volcengine.com/contact/product?t=%E5%AF%B9%E8%AF%9D%E5%BC%8Fai&source=%E4%BA%A7%E5%93%81%E5%92%A8%E8%AF%A2',
                '_blank'
              )
            }
          >
            联系我们
          </div>
        </div>
      )}
    </div>
  );
}

export default Header;
