import { useId, useState } from 'react';

import logo_frame_url from '../../../assets/figma/logo-frame.svg';
import logo_glyph_url from '../../../assets/figma/logo-glyph.svg';
import start_icon_url from '../../../assets/figma/header-start.svg';
import stop_icon_url from '../../../assets/figma/header-stop-icon.svg';
import { Button } from '../../../shared/ui';
import type { BackendBinanceConnectionStatus } from '../../../shared/contracts';

import styles from './AppHeader.module.css';

export interface AppHeaderProps {
  connectionDetails?: BackendBinanceConnectionStatus | null;
  connectionDetailsError?: boolean;
  isConnected: boolean;
  isTrading: boolean;
  hasOpenPosition: boolean;
  isCommandPending: boolean;
  onConnectionDetailsOpenChange?: (open: boolean) => void;
  onStartRequested: () => void;
  onStopRequested: () => void;
}

/**
 * 함수 이름: AppHeader()
 * 기능: 브랜드, API 연결 상태와 자동매매 및 복구 Position 청산 제어를 표시한다.
 * 인자: props -> 연결·거래·Position·명령 대기 상태와 사용자 의도 처리 함수
 * 반환값: Figma 상단 상태 표시줄 요소
 * 작성 날짜: 2026/08/12
 */
export function AppHeader({
  connectionDetails = null,
  connectionDetailsError = false,
  isConnected,
  isTrading,
  hasOpenPosition,
  isCommandPending,
  onConnectionDetailsOpenChange,
  onStartRequested,
  onStopRequested,
}: AppHeaderProps) {
  const connection_tooltip_id = useId();
  const [is_connection_tooltip_open, set_is_connection_tooltip_open] = useState(false);
  // 정지 상태의 열린 Position은 재시작으로 복구된 exposure이므로 시작 대신 청산만 허용한다.
  const has_recovered_position = !isTrading && hasOpenPosition;
  const stop_button_label = has_recovered_position ? '복구 포지션 청산' : '매매 중지';

  /**
   * 함수 이름: set_connection_tooltip_open()
   * 기능: hover·focus 수명주기와 앱의 연결 상태 조회를 함께 갱신한다.
   * 인자: open -> 툴팁 표시 여부
   * 반환값: 없음
   * 작성 날짜: 2026/09/05
   */
  function set_connection_tooltip_open(open: boolean): void {
    set_is_connection_tooltip_open(open);
    onConnectionDetailsOpenChange?.(open);
  }

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
        <span className={styles.connectionAnchor}>
          <button
            aria-describedby={is_connection_tooltip_open ? connection_tooltip_id : undefined}
            aria-label={`Binance 연결 상태: ${isConnected ? 'LIVE' : 'OFFLINE'}`}
            className={`${styles.connection} ${isConnected ? styles.online : styles.offline}`}
            onBlur={() => set_connection_tooltip_open(false)}
            onFocus={() => set_connection_tooltip_open(true)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                set_connection_tooltip_open(false);
              }
            }}
            onMouseEnter={() => set_connection_tooltip_open(true)}
            onMouseLeave={() => set_connection_tooltip_open(false)}
            type="button"
          >
            <span className={styles.dot} aria-hidden="true" />
            {isConnected ? 'LIVE' : 'OFFLINE'}
          </button>
          {is_connection_tooltip_open && (
            <div className={styles.connectionTooltip} id={connection_tooltip_id} role="tooltip">
              <strong className={styles.connectionTooltipTitle}>Binance 연결 상태</strong>
              <dl className={styles.connectionDetails}>
                {([
                  ['API', 'api'],
                  ['시세 WebSocket', 'market_stream'],
                  ['계좌 WebSocket', 'account_stream'],
                ] as const).map(([label, key]) => {
                  const status = connectionDetails?.[key];
                  const status_label = status === 'online'
                    ? '연결됨'
                    : status === 'offline'
                      ? '연결 안 됨'
                      : connectionDetailsError ? '확인 불가' : '확인 중';

                  return (
                    <div className={styles.connectionRow} key={key}>
                      <dt>{label}</dt>
                      <dd className={styles.connectionStatus} data-state={status}>
                        <span aria-hidden="true" className={styles.statusDot} />
                        {status_label}
                      </dd>
                    </div>
                  );
                })}
              </dl>
            </div>
          )}
        </span>
      </div>

      <div className={styles.actions}>
        <Button
          className={styles.headerButton}
          disabled={(!isTrading && !has_recovered_position) || isCommandPending}
          onClick={onStopRequested}
          tone="negative"
        >
          <img className={styles.actionIcon} src={stop_icon_url} alt="" />
          {stop_button_label}
        </Button>
        <Button
          className={styles.headerButton}
          disabled={isTrading || has_recovered_position || isCommandPending}
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
