/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useSelector } from 'react-redux';
import AudioLoading from '@/components/Loading/AudioLoading';
import { RootState } from '@/store';
import { useDeviceState } from '@/lib/useCommon';
import style from './index.module.less';

const THRESHOLD_VOLUME = 18;

function AudioController(props: React.HTMLAttributes<HTMLDivElement>) {
  const { className, ...rest } = props;
  const room = useSelector((state: RootState) => state.room);
  const volume = room.localUser.audioPropertiesInfo?.linearVolume || 0;
  const { isAudioPublished } = useDeviceState();
  const isLoading = volume >= THRESHOLD_VOLUME && isAudioPublished;
  return (
    <div className={`${className}`} {...rest}>
      {isAudioPublished ? (
        isLoading ? null : (
          <div className={style.closed}>请开始说话</div>
        )
      ) : (
        <div className={style.closed}>你已关闭麦克风</div>
      )}
      <AudioLoading loading={isLoading} color={isAudioPublished ? undefined : '#EAEDF1'} />
    </div>
  );
}
export default AudioController;
