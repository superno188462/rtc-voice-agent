/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useEffect } from 'react';
import { useDispatch } from 'react-redux';
import { Message } from '@arco-design/web-react';
import Header from '@/components/Header';
import ResizeWrapper from '@/components/ResizeWrapper';
import Menu from './Menu';
import { useIsMobile } from '@/utils/utils';
import Apis from '@/app/index';
import MainArea from './MainArea';
import { ABORT_VISIBILITY_CHANGE, useLeave } from '@/lib/useCommon';
import useMockAgent from '@/lib/useMockAgent';
import {
  RTCConfig,
  SceneConfig,
  updateRTCConfig,
  updateScene,
  updateSceneConfig,
} from '@/store/slices/room';
import styles from './index.module.less';

export default function () {
  useMockAgent();

  const leaveRoom = useLeave();
  const dispatch = useDispatch();

  const getScenes = async () => {
    try {
      const {
        scenes,
      }: {
        scenes: {
          rtc: RTCConfig;
          scene: SceneConfig;
        }[];
      } = await Apis.Basic.getScenes();
      if (!scenes?.length) {
        Message.error(
          '没有可用场景。请在 python_server/scenes 中配置 Custom.json 并启动 Python 后端。'
        );
        return;
      }
      dispatch(updateScene(scenes[0].scene.id));
      dispatch(
        updateSceneConfig(
          scenes.reduce<Record<string, SceneConfig>>((prev, cur) => {
            prev[cur.scene.id] = cur.scene;
            return prev;
          }, {})
        )
      );
      dispatch(
        updateRTCConfig(
          scenes.reduce<Record<string, RTCConfig>>((prev, cur) => {
            prev[cur.scene.id] = cur.rtc;
            return prev;
          }, {})
        )
      );
    } catch (error) {
      Message.error(`场景加载失败，请确认 Python 后端已启动：${String(error)}`);
    }
  };

  useEffect(() => {
    getScenes();
    const isOriginalDemo = window.location.host.startsWith('localhost');
    const handler = () => {
      if (
        document.visibilityState === 'hidden' &&
        !sessionStorage.getItem(ABORT_VISIBILITY_CHANGE)
      ) {
        leaveRoom();
      }
    };
    !isOriginalDemo && document.addEventListener('visibilitychange', handler);
    return () => {
      !isOriginalDemo && document.removeEventListener('visibilitychange', handler);
    };
  }, []);

  return (
    <ResizeWrapper className={styles.container}>
      <Header />
      <div
        className={styles.main}
        style={{
          padding: useIsMobile() ? '' : '24px',
        }}
      >
        <div className={`${styles.mainArea} ${useIsMobile() ? styles.isMobile : ''}`}>
          <MainArea />
        </div>
        {useIsMobile() ? null : (
          <div className={styles.operationArea}>
            <Menu />
          </div>
        )}
      </div>
    </ResizeWrapper>
  );
}
