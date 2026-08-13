import logo_frame_url from '../../../assets/figma/logo-frame.svg';
import logo_glyph_url from '../../../assets/figma/logo-glyph.svg';
import start_icon_url from '../../../assets/figma/header-start.svg';
import stop_icon_url from '../../../assets/figma/header-stop-icon.svg';
import { Button } from '../../../shared/ui';

import styles from './AppHeader.module.css';

export interface AppHeaderProps {
  isConnected: boolean;
  isTrading: boolean;
  isCommandPending: boolean;
  onStartRequested: () => void;
  onStopRequested: () => void;
}

/**
 * 함수 이름: AppHeader()
 * 기능: 브랜드, API 연결 상태와 자동매매 시작 및 중지 제어를 표시한다.
 * 인자: props -> 연결 상태, 자동매매 상태, 명령 대기 상태와 사용자 의도 처리 함수
 * 반환값: Figma 상단 상태 표시줄 요소
 * 작성 날짜: 2026/08/12
 */
export function AppHeader({
  isConnected,
  isTrading,
  isCommandPending,
  onStartRequested,
  onStopRequested,
}: AppHeaderProps) {
  return (
    <header className={styles.header}>
      <div className={styles.brand} aria-label="ETH AUTO Binance Trading">
        <span className={styles.logo} aria-hidden="true">
          <img className={styles.logoFrame} src={logo_frame_url} alt="" />
          <img className={styles.logoGlyph} src={logo_glyph_url} alt="" />
        </span>
        <span className={styles.brandCopy}>
          <strong>ETH AUTO</strong>
          <small>Binance TRADING</small>
        </span>
        <span className={`${styles.connection} ${isConnected ? styles.online : styles.offline}`}>
          <span className={styles.dot} aria-hidden="true" />
          {isConnected ? 'LIVE' : 'OFFLINE'}
        </span>
      </div>

      <div className={styles.actions}>
        <Button
          className={styles.headerButton}
          disabled={!isTrading || isCommandPending}
          onClick={onStopRequested}
          tone="negative"
        >
          <img className={styles.actionIcon} src={stop_icon_url} alt="" />
          매매 중지
        </Button>
        <Button
          className={styles.headerButton}
          disabled={isTrading || isCommandPending}
          onClick={onStartRequested}
          tone="positive"
        >
          <img className={styles.actionIcon} src={start_icon_url} alt="" />
          {isTrading ? '자동매매 실행 중' : '자동매매 실행'}
        </Button>
      </div>
    </header>
  );
}
