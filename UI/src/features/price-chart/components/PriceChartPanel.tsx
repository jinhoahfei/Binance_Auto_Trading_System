import { useEffect } from 'react';

import { ChartCanvas } from './ChartCanvas';
import { ChartToolbar } from './ChartToolbar';
import { IndicatorSettingsPopover } from './IndicatorSettingsPopover';
import type { PriceChartPanelProps } from '../types';
import styles from './PriceChartPanel.module.css';

const status_label_by_data_status = {
    idle: '대기',
    loading: '동기화',
    live: 'LIVE',
    reconnecting: '재연결',
    error: '오프라인',
} as const;

// Figma frame 03은 차트 선과 별개로 세 toggle이 모두 꺼진 정적 팝오버를 요구한다.
const FIXTURE_POPOVER_INDICATOR_SETTINGS = {
    bollingerBand: false,
    ema9: false,
    volume: false,
} as const;


/**
 * 함수 이름: format_chart_symbol()
 * 기능: Binance symbol을 차트 부제에 사용할 거래쌍 표기로 변환한다.
 * 인자: symbol -> Binance 거래 symbol
 * 반환값: 구분 기호가 포함된 거래쌍 문자열
 * 작성 날짜: 2026/08/20
 */
function format_chart_symbol(symbol: string): string {
    if (symbol.endsWith('USDT')) {
        return `${symbol.slice(0, -4)}/USDT`;
    }

    return symbol;
}


/**
 * 함수 이름: PriceChartPanel()
 * 기능: ETH 차트 제목, 주기 도구 모음과 Lightweight Charts 기반 캔들 차트를 표시한다.
 * 인자: props -> 가격 차트 ViewModel과 사용자 intent 처리 함수
 * 반환값: ETH 가격 차트 패널 React 요소
 * 작성 날짜: 2026/08/20
 */
export function PriceChartPanel({
    activeState,
    bollingerLower,
    bollingerUpper,
    candles,
    contextMenuPosition = null,
    dataStatus = 'idle',
    dataRevision = 0,
    drawingActive = false,
    drawings = [],
    ema,
    historyErrorMessage = null,
    historyExhausted = false,
    historyLoading = false,
    indicatorSettings,
    indicatorSettingsOpen = false,
    interval,
    isFullscreen = false,
    lineContextMenuOpen = false,
    onLoadEarlier,
    onIntent,
    presentationMode = 'interactive',
    position_average_entry_price = null,
    selectedLineId = null,
    statusMessage = null,
    symbol = 'ETHUSDT',
    timestampLabel,
}: PriceChartPanelProps) {
    const status_tone_class = dataStatus === 'live'
        ? styles.liveStatus
        : dataStatus === 'error'
            ? styles.errorStatus
            : styles.pendingStatus;

    useEffect(() => {
        // 설정창이 열린 동안만 바깥 클릭을 관찰하고 닫히면 listener를 제거한다.
        if (!indicatorSettingsOpen) {
            return undefined;
        }

        /**
         * 함수 이름: handle_outside_pointer_down()
         * 기능: 지표 설정창과 열기 버튼 바깥 클릭에서만 닫기 intent를 전달한다.
         * 인자: event -> 문서 pointerdown 이벤트
         * 반환값: 없음
         * 작성 날짜: 2026/09/17
         */
        const handle_outside_pointer_down = (event: PointerEvent) => {
            if (!(event.target instanceof Element)
                || event.target.closest('[data-indicator-settings-popover]') !== null
                || event.target.closest('[data-indicator-settings-trigger]') !== null) {
                return;
            }

            onIntent?.({ type: 'INDICATOR_SETTINGS_CLOSED' });
        };

        document.addEventListener('pointerdown', handle_outside_pointer_down);

        return () => document.removeEventListener('pointerdown', handle_outside_pointer_down);
    }, [indicatorSettingsOpen, onIntent]);

    // Fixture에서는 8월 12일 기준에 없던 live status와 symbol 접두사를 렌더하지 않는다.
    return (
        <section
            aria-labelledby="price-chart-title"
            className={`${styles.panel} ${isFullscreen ? styles.fullscreenPanel : ''}`}
        >
            <div className={styles.header}>
                <div className={styles.heading}>
                    <div className={styles.titleRow}>
                        <h2 id="price-chart-title">ETH 가격 차트</h2>
                        {presentationMode === 'interactive' ? (
                            <span
                                aria-live="polite"
                                className={`${styles.dataStatus} ${status_tone_class}`}
                            >
                                {status_label_by_data_status[dataStatus]}
                            </span>
                        ) : null}
                    </div>
                    <p>
                        {presentationMode === 'interactive'
                            ? `${format_chart_symbol(symbol)} · ${timestampLabel}`
                            : timestampLabel}
                    </p>
                </div>
                <ChartToolbar
                    activeState={activeState}
                    indicatorSettingsOpen={indicatorSettingsOpen}
                    interval={interval}
                    onIntent={onIntent}
                />
            </div>
            <ChartCanvas
                bollingerLower={bollingerLower}
                bollingerUpper={bollingerUpper}
                candles={candles}
                dataRevision={dataRevision}
                contextMenuPosition={contextMenuPosition}
                dataStatus={dataStatus}
                drawingActive={drawingActive}
                drawings={drawings}
                ema={ema}
                historyErrorMessage={historyErrorMessage}
                historyExhausted={historyExhausted}
                historyLoading={historyLoading}
                indicatorSettings={indicatorSettings}
                interval={interval}
                isFullscreen={isFullscreen}
                lineContextMenuOpen={lineContextMenuOpen}
                onLoadEarlier={onLoadEarlier}
                onIntent={onIntent}
                presentationMode={presentationMode}
                position_average_entry_price={position_average_entry_price}
                selectedLineId={selectedLineId}
                statusMessage={statusMessage}
                symbol={symbol}
            />
            {indicatorSettingsOpen && indicatorSettings !== undefined ? (
                <IndicatorSettingsPopover
                    onIntent={onIntent}
                    settings={presentationMode === 'fixture'
                        ? FIXTURE_POPOVER_INDICATOR_SETTINGS
                        : indicatorSettings}
                />
            ) : null}
        </section>
    );
}
