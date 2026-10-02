/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import React, { useRef, useEffect } from 'react';
import { useSelector } from 'react-redux';
import { Tag, Spin } from '@arco-design/web-react';
import { RootState } from '@/store';
import Loading from '@/components/Loading/HorizonLoading';
import { isMobile } from '@/utils/utils';
import { useScene } from '@/lib/useCommon';
import USER_AVATAR from '@/assets/img/userAvatar.png';
import styles from './index.module.less';
import AIAvatarReadying from '@/components/AIAvatarLoading';

const lines: (string | React.ReactNode)[] = [];

function Conversation(props: React.HTMLAttributes<HTMLDivElement> & { showSubtitle: boolean }) {
  const { className, showSubtitle, ...rest } = props;
  const room = useSelector((state: RootState) => state.room);
  const { msgHistory, isFullScreen, asrStatus, asrPartialText } = room;
  const { userId } = useSelector((state: RootState) => state.room.localUser);
  const { isAITalking, isUserTalking, scene } = useSelector((state: RootState) => state.room);
  const isAIReady = msgHistory.length > 0;
  const containerRef = useRef<HTMLDivElement>(null);
  const { botName, icon, isAvatarScene } = useScene();

  const isUserTextLoading = (owner: string) => {
    return owner === userId && isUserTalking;
  };

  const isAITextLoading = (owner: string) => {
    return (owner === botName || owner.includes('voiceChat_')) && isAITalking;
  };

  useEffect(() => {
    const container = containerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight - container.clientHeight;
    }
  }, [msgHistory.length]);

  return (
    <div
      ref={containerRef}
      className={`${styles.conversation} ${className} ${isFullScreen ? styles.fullScreen : ''} ${
        isMobile() ? styles.mobileConversation : ''
      }`}
      style={isAvatarScene && !isAIReady ? { justifyContent: 'center' } : {}}
      {...rest}
    >
      {lines.map((line) => line)}
      {!isAIReady ? (
        <div className={styles.aiReadying}>
          {isAvatarScene ? (
            <AIAvatarReadying />
          ) : asrStatus === 'listening' ? (
            asrPartialText ? (
              `正在识别：${asrPartialText}`
            ) : (
              '已连接，正在聆听，请开始说话'
            )
          ) : asrStatus === 'no_audio' ? (
            'ASR 已连接，但没有收到麦克风音频。请开启麦克风后重试。'
          ) : asrStatus === 'error' ? (
            'ASR 连接失败，请检查后端配置并重新加入房间。'
          ) : (
            <>
              <Spin size={16} className={styles['aiReading-spin']} />
              {asrStatus === 'connecting' ? 'ASR 连接中，请稍候' : 'AI 准备中，请稍候'}
            </>
          )}
        </div>
      ) : (
        ''
      )}
      {(showSubtitle ? msgHistory : [])?.map(({ value, user, isInterrupted }, index) => {
        const isUserMsg = user === userId;
        const isRobotMsg = user === botName || user.includes('voiceChat_');
        if (!isUserMsg && !isRobotMsg) {
          return '';
        }
        return (
          <div
            key={`msg-container-${index}`}
            className={styles.mobileLine}
            style={{ justifyContent: isUserMsg && isMobile() ? 'flex-end' : '' }}
          >
            {!isMobile() && (
              <div className={styles.msgName}>
                <div className={styles.avatar}>
                  <img src={isUserMsg ? USER_AVATAR : icon} alt="Avatar" />
                </div>
                {isUserMsg ? '我' : scene}
              </div>
            )}
            <div
              className={`${styles.sentence} ${isUserMsg ? styles.user : styles.robot}`}
              key={`msg-${index}`}
            >
              <div className={styles.content}>
                {value}
                <div className={styles['loading-wrapper']}>
                  {isAIReady &&
                  (isUserTextLoading(user) || isAITextLoading(user)) &&
                  index === msgHistory.length - 1 ? (
                    <Loading gap={3} className={styles.loading} dotClassName={styles.dot} />
                  ) : (
                    ''
                  )}
                </div>
              </div>
              {!isUserMsg && isInterrupted ? <Tag className={styles.interruptTag}>已打断</Tag> : ''}
            </div>
          </div>
        );
      })}
      {isAIReady && asrPartialText ? (
        <div className={styles.aiReadying}>正在识别：{asrPartialText}</div>
      ) : null}
    </div>
  );
}

export default Conversation;
